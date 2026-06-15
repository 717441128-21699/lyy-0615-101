# 崩溃安全的原子文件写入工具

一个 Python 实现的生产级原子文件写入工具，解决"直接覆盖写 + 中途断电 = 损坏文件"的经典问题。

采用标准的 **"写临时文件 → fsync → 原子 rename → 父目录 fsync"** 四步协议，确保在任意时刻崩溃，目标文件要么是完整的旧内容，要么是完整的新内容，绝不会处于半写的损坏状态。

---

## 快速索引

- [安装与 CLI 入口](#安装与-cli-入口)
- [子命令速览](#子命令速览)
- [1. 单文件写入 (write)](#1-单文件写入-write)
- [2. 诊断 (doctor)](#2-诊断-doctor)
- [3. 预检/预演 (--check / --dry-run)](#3-预检--dry-run---check---dry-run)
- [4. 批量写入 (batch)](#4-批量写入-batch)
- [退出码总表](#退出码总表)
- [常见场景选择指南](#常见场景选择指南)
- [核心原理详解](#核心原理详解)
- [严格模式的失败分类](#严格模式的失败分类)
- [API 参考](#api-参考)

---

## 安装与 CLI 入口

```bash
# 安装后可直接敲 atomic-write
pip install .

# 验证
atomic-write doctor
```

不安装的替代方式：
```bash
python -m atomic_file doctor
python atomic_file.py doctor .
```

---

## 子命令速览

| 子命令 | 作用 | 典型用途 |
|--------|------|---------|
| `write` (默认，可省略) | 原子写入单个文件 | 日常配置写入、日志轮转 |
| `doctor` | 诊断当前/指定目录的崩溃安全能力 | CI 启动预检、部署检查 |
| `batch` | 从 JSON 清单一次写入多个目标文件 | 部署时批量更新配置、应用配置清单 |

```bash
# 显式子命令
atomic-write write config.json --text '{}'
# 等价于默认形式（推荐省略，更简洁）
atomic-write config.json --text '{}'
```

---

## 1. 单文件写入 (write)

### 基础用法

```bash
# 直接文本
atomic-write config.json --text '{"key": "value"}'

# 从源文件安全替换
atomic-write production.sql --file /tmp/new_schema.sql

# 从 stdin 管道
cat large_dump.json | atomic-write output.json --file -

# 二进制 + 权限
atomic-write snapshot.db --file /tmp/snap.db --binary --permissions 640
```

### 严格模式 vs 默认模式

| 模式 | 目录 fsync 失败时行为 | 适用 |
|------|----------------------|------|
| **默认** | 降级成功，退出码 2，提示警告 | 多数应用：写入成功比完美保证更重要 |
| `--no-degraded` | 报错退出码 4，明确说明内容已替换但缺目录 fsync | 想要知道每一步是否 100% 完成 |
| `--strict` | 同上，退出码 4 | 最严格场景 |

⚠️ **退出码 4 的准确含义**：
> 文件内容 **已经替换成功**（rename 已原子完成），临时文件 fsync 也成功（数据块在磁盘上）。**唯一缺失**是目录项 fsync——如果**立刻断电**，重启后目录项可能回滚。如果不立刻断电，操作系统几秒内就会把目录项刷盘，之后就完全安全。**绝对不能**在退出码 4 时说"旧文件未变"。

详见 [严格模式的失败分类](#严格模式的失败分类)。

---

## 2. 诊断 (doctor)

在 CI 启动或部署前跑一次，明确告诉你这个目录的崩溃安全承诺等级。

```bash
# 诊断当前目录
atomic-write doctor

# 诊断指定目录
atomic-write doctor /path/to/config/dir

# CI 友好模式（只看退出码）
atomic-write doctor . --quiet
if [ $? -ne 0 ]; then
  echo "::warning::Directory does not support full crash-safety, writes will be degraded"
fi

# 机器可读 JSON
atomic-write doctor . --json
```

**输出示例**：
```
Crash-safety diagnosis for: D:\project\configs
Platform: win32

✓ Parent directory exists: D:\project\configs
✓ Same-filesystem atomic rename: PASS: rename() will be atomic within the directory
✗ Directory fsync (entry persistence): FAIL: [Errno 5] FlushFileBuffers failed...
✓ Stdin reading support: Available (can use --file -)
✓ Temp file creation: Can create temp files in D:\project\configs

Overall: Degraded mode expected. Missing/failing capabilities: Directory fsync...
```

**doctor 检查的能力**：
1. 父目录是否存在
2. 同一文件系统（rename 能否原子）
3. 目录 fsync 支持性（能否完全持久化目录项）
4. stdin 可用性
5. 临时文件创建权限

**CI 退出码**：
| 退出码 | 含义 |
|--------|------|
| 0 | 全部能力可用，完整崩溃安全 |
| 2 | 部分能力缺失，降级模式（可写但需注意风险） |

---

## 3. 预检 / 预演 (--check / --dry-run)

**`--check`**：只检测崩溃安全能力，不处理输入。

**`--dry-run`**：接近真实写入——校验输入、权限设置、预测是完整安全还是降级，但**不改目标文件**。

```bash
# 只检查目录能力
atomic-write --check config.json

# 预演：检查文本编码、权限、预测结果
atomic-write --dry-run config.json --text '{"key": "value"}' --permissions 644

# 预演：检查源文件是否存在、大小、编码
atomic-write --dry-run data.bin --file /tmp/source.bin --binary
```

**`--dry-run` 输出示例**：
```
Dry-run for target: D:\project\config.json
  [Crash safety]
    Parent dir exists  : YES
    Same filesystem    : YES (atomic rename)
    Directory fsync    : NOT SUPPORTED → [Errno 5] ...
  [Input validation]
    ✓ Input --text (encoding=utf-8): OK: 18 bytes would be written
    ✓ Permissions      : 0o644
  [Prediction]
    Write outcome      : DEGRADED success (exit 2), missing: dir fsync → degraded mode
    Target file        : WOULD NOT be modified

Dry-run complete. No files were modified.
```

---

## 4. 批量写入 (batch)

从 JSON 清单一次写入多个目标文件，每个独立执行原子写入，失败不影响已成功的项。

### 清单格式 (manifest.json)

```json
[
  {"target": "config/app.json",
   "text": "{\"port\": 8080}",
   "permissions": 420},

  {"target": "data/snapshot.db",
   "file": "/tmp/snap.db",
   "binary": true},

  {"target": "docs/readme_cn.txt",
   "file": "/tmp/README.md",
   "encoding": "gbk"}
]
```

字段：
- `target` (str, 必需)：目标路径
- `text` (str)：直接写文本
- `file` (str)：从文件读取；与 `text` 二选一
- `encoding` (str, 默认 utf-8)：文本编码
- `binary` (bool, 默认 false)：二进制模式（仅与 `file` 同用）
- `permissions` (int)：八进制权限数值（如 `0o644` = 420）

### 执行

```bash
# 标准执行
atomic-write batch manifest.json

# 严格模式（每个文件的目录 fsync 失败都算该项失败）
atomic-write batch manifest.json --strict

# 机器可读
atomic-write batch manifest.json --json

# CI 中只关心退出码
atomic-write batch manifest.json --quiet
rc=$?
```

**输出示例**：
```
Batch result: total=3  succeeded=1  degraded=1  failed=1
  ✓ [success ]     18B [    safe] → config/app.json
  ~ [degraded]  10240B [degraded] → data/snapshot.db
  ✗ [failed  ]      0B [     N/A] → docs/readme_cn.txt
         error: ValueError: invalid encoding 'gbk'
         NOTE: target was NOT modified
```

**批量退出码**：
| 退出码 | 含义 |
|--------|------|
| 0 | 全部完全成功（无降级无失败） |
| 5 | 部分成功（有失败或有降级）—— CI 中可视为失败 |
| 6 | 全部失败 |

批量操作**不会回滚已成功的项**——这是设计选择：每个目标都是独立的原子操作，完成了就持久化。

---

## 退出码总表

| 码 | 常量名 | 含义 | 目标文件状态 |
|----|--------|------|-------------|
| 0 | `EXIT_SUCCESS` | 成功，全部 fsync 完成 | 已替换，完全崩溃安全 |
| 1 | `EXIT_WRITE_FAILED` | **rename 之前**失败 | **未修改**，仍是旧内容 |
| 2 | `EXIT_DEGRADED_SUCCESS` | 成功但降级 | 已替换，目录项 fsync 缺失 |
| 3 | `EXIT_INVALID_ARGS` | 参数错误 | 未修改 |
| 4 | `EXIT_POST_RENAME_FAILURE` | **rename 已完成**、目录 fsync 失败、且 strict/no-degraded | **已替换**，但目录项未持久化 |
| 5 | `EXIT_PARTIAL_BATCH_FAILURE` | 批量：部分成功/部分失败 | 各目标独立 |
| 6 | `EXIT_TOTAL_BATCH_FAILURE` | 批量：全部失败 | 各目标独立 |

---

## 常见场景选择指南

| 场景 | 推荐调用 | 承诺等级 |
|------|---------|---------|
| **配置文件更新** | `atomic-write config.json --file new.json` | ✅ 不半写；Linux 完整安全 / Windows 降级 |
| **小数据库快照** | `atomic-write snapshot.db --file tmp.db --strict` | ✅ 不半写 ✅ 退出码 0/4 明确分开 |
| **日志轮转** | `atomic-write current.log --file rotated.log` | ✅ 不半写 ⚠️ 降级可接受 |
| **部署预检** | `atomic-write doctor /config --quiet` | 0 或 2 决定部署策略 |
| **批量配置更新** | `atomic-write batch changes.json` | 每文件独立；退出码 5/6 触发告警 |

### 配置文件

```bash
# 标准方式
atomic-write /etc/myapp/config.json --file /tmp/new_config.json
# 退出码 0/2 → 写入成功，根据退出码决定是否告警
```

**承诺**：绝不会半写损坏。Linux ext4/xfs 通常完整安全，Windows NTFS 通常降级。

### 数据库快照

```bash
atomic-write /data/snapshot.db --file /tmp/snapshot.tmp --strict
case $? in
  0) echo "Snapshot safely persisted" ;;
  4) echo "Snapshot written but dir fsync skipped; WAIT 5s before power-cycling!" ;;
  1) echo "Snapshot FAILED, old snapshot intact, retry!" ;;
  *) echo "Unknown error" ;;
esac
```

**退出码 4 的处理**：不要重试——快照已经替换了！只要不立刻断电就安全。通常 sleep 几秒让 OS 刷目录项即可。

### 日志轮转

```bash
atomic-write /var/log/app/current.log --file /tmp/rotated.log
# 退出码 2 也没关系，日志不会损坏
```

### CI 部署预检

```yaml
# GitHub Actions 示例
- name: Check config dir crash-safety
  run: |
    atomic-write doctor /etc/myapp --quiet
    if [ $? -eq 2 ]; then
      echo "::warning::Directory /etc/myapp doesn't fully support crash-safe writes"
    fi
```

### 批量配置更新

```bash
atomic-write batch deploy_changes.json
# 退出码 5 意味着至少 1 项失败或降级
if [ $? -eq 5 ]; then
  atomic-write batch deploy_changes.json --json > /tmp/last_batch.json
  echo "Some items failed! See /tmp/last_batch.json"
  exit 1
fi
```

---

## 核心原理详解

### 问题：为什么直接覆盖写不安全？

```
应用 write() → 内核页缓存 → pdflush 异步刷盘 → 磁盘控制器缓存 → 物理介质
```

中间任意一步断电 → 文件半截、大小为 0、或块部分更新 → **既不是旧也不是新**。

### 四步原子写入协议

```
① mkstemp(dir=父目录)      在同一目录建临时文件→同 FS→rename 原子
② write(data)              全部数据写入临时文件
③ fsync(临时文件)          数据块+inode 落盘 → 防 write/rename 之间断电
④ os.replace → rename      原子切换 inode 指针 → 任何时刻要么旧要么新
⑤ fsync(父目录)            目录项落盘 → 防 rename/返回之间断电
⑥ finally 清理              任一步失败删临时文件
```

### rename 原子性（同一 FS 内）

POSIX 规定：`rename()` 对观察者是原子的——任何时刻要么看到旧文件要么看到新文件，没有中间态。实现上：
- 拿父目录 inode 锁
- 在一个日志事务（ext4 jbd2/xfs 日志）内：删旧目录项、加新目录项、改硬链接计数
- 事务整体提交或回滚

**前提（极其重要）**：源和目标必须在**同一挂载点**。跨 FS 时，Linux 内核会静默退化成 copy+unlink，完全非原子。

### 临时文件 fsync 的作用

```
write(tmp) → [崩溃] → rename → fsync(dir)
```
断电时刻在 write 完成但 fsync 未做 → inode 指向的块是旧垃圾/全零 → 内容损坏。

### 父目录 fsync 的作用

```
write(tmp) → fsync(tmp) → rename → [崩溃] → fsync(dir)
```
断电时刻在 rename 返回但 fsync(dir) 未做 → 目录项在 Page Cache → 重启后目录项回滚 → 新建文件"消失"、覆盖写回退到旧内容。

### 各步骤防御崩溃时刻汇总

| 步骤 | 漏掉后最坏后果 |
|------|---------------|
| mkstemp(dir=parent) | 跨 FS → rename 退化为 copy+unlink → 中途崩溃损坏 |
| fsync(临时文件) | inode 指向垃圾块 → rename 后文件**内容损坏** |
| rename | 本身保证原子，无中间态 |
| fsync(父目录) | 目录项回滚 → 文件"消失"或回滚到旧内容 |

---

## 严格模式的失败分类

`atomic_write()` 抛出的 `AtomicWriteError` 带有 `phase` 字段和 `target_modified` 属性，调用者能精确分类。

### 两种失败阶段

| 阶段 | `FailPhase` | 退出码 | 含义 | `target_modified` |
|------|-------------|--------|------|-------------------|
| **前** rename | `BEFORE_RENAME` | 1 | mkstemp / write / fsync(tmp) / rename 自身 等步骤失败 | `False` —— 旧内容完全完好 |
| **后** rename | `AFTER_RENAME` | 4 | 只有目录项 fsync 失败 | `True` —— 新内容已经替换 |

**对应终端提示**：
- BEFORE_RENAME：`Target file was NOT modified (still contains old content).`
- AFTER_RENAME：
  ```
  IMPORTANT: The target file HAS been replaced with new content, but directory entry fsync failed.
  Temporary file fsync succeeded (data blocks are on disk).
  Risk: if power fails NOW, directory entry may revert, causing the file to appear as old content or disappear.
  ```

### 精确到每一步的失败（API 级别）

[AtomicWriteError.phase](file:///d:/trae-bz/TraeProjects/101/atomic_file.py#L51-L54) 在错误消息里明确标出错在哪一步：

| 场景 | `AtomicWriteError.phase` | 对应提示前缀 |
|------|--------------------------|-------------|
| 临时文件创建失败 | BEFORE_RENAME | `Failed to create temporary file:` |
| 临时文件 write 失败 | BEFORE_RENAME | `Failed to write/flush temporary file:` |
| **临时文件 fsync 失败** | BEFORE_RENAME | **`Failed to fsync temporary file (data may not be persisted):`** |
| rename 自身失败 | BEFORE_RENAME | `Failed to rename temp file to target:` |
| **目录项 fsync 失败** | **AFTER_RENAME** | **`Directory fsync failed (strict mode):`** |

可以看到——**临时文件 fsync 失败是 BEFORE_RENAME**（目标未修改），**只有目录 fsync 失败才是 AFTER_RENAME**（内容已替换）。这正是需求 3 要求的精确分类。

---

## API 参考

### `atomic_write()`

```python
atomic_write(
    path: Union[str, Path],
    data: Union[bytes, str],
    encoding: Optional[str] = None,
    permissions: Optional[int] = None,
    allow_degraded: bool = True,
    strict: bool = False,
) -> AtomicWriteResult
```

### `check_crash_safety()`

```python
check_crash_safety(target: Union[str, Path]) -> CheckResult
```

### `doctor()`

```python
doctor(directory: Optional[Union[str, Path]] = None) -> DoctorResult
```

### `batch_write()`

```python
batch_write(
    manifest: Union[str, Path, List[Dict]],
    allow_degraded: bool = True,
    strict: bool = False,
) -> BatchResult
```

### 异常

```python
AtomicWriteError(
    phase: str,            # 'before_rename' | 'after_rename'
    message: str,
    target_path: Optional[Path] = None,
)
# 属性: target_modified -> bool
```

### 结果类型

- `AtomicWriteResult` —— 单文件写入结果
- `CheckResult` —— 单个目标预检结果
- `DoctorResult` —— 目录诊断结果
- `BatchResult` / `BatchItemResult` —— 批量结果

---

## 测试

```bash
python test_atomic_file.py -v
```

---

## 常见误区速查

| ❌ 误区 | ✅ 事实 |
|---------|---------|
| "rename 原子了就不用 fsync" | 漏 fsync(tmp) → 内容损坏 |
| "fsync 文件就够了，目录不用" | 漏 fsync(dir) → 目录项回滚 → 文件"消失" |
| "strict 模式失败 = 旧文件不变" | 退出码 4 时 rename **已经完成**，内容已替换，只是缺目录项 fsync |
| "跨 FS rename 也原子" | 内核退化为 copy+unlink，完全非原子 |
| "降级模式 = 写入失败" | 降级模式下数据已完整写入，只是断电回滚保护不完整 |
| "批量写入会全部回滚" | 每个文件独立原子操作，已成功的不回滚 |
