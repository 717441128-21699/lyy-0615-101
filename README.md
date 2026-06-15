# 崩溃安全的原子文件写入工具

一个 Python 实现的生产级原子文件写入工具，解决"直接覆盖写 + 中途断电 = 损坏文件"的经典问题。

采用标准的 **"写临时文件 → fsync → 原子 rename → 父目录 fsync"** 四步协议，确保在任意时刻崩溃，目标文件要么是完整的旧内容，要么是完整的新内容，绝不会处于半写的损坏状态。

---

## 安装与使用

### 安装（推荐）

```bash
# 在项目目录下安装，之后可直接敲 atomic-write 命令
pip install .

# 验证安装
atomic-write --check /tmp/test.txt
```

安装后 `atomic-write` 命令可直接在终端使用，无需 `python -m`。

### 不安装直接运行

```bash
# 方式 1：python -m
python -m atomic_file config.json --text '{"key": "value"}'

# 方式 2：直接运行（需要 python 在 PATH 中，且文件有 .py 扩展名）
python atomic_file.py config.json --text '{"key": "value"}'

# 方式 3（Linux/macOS）：创建别名
alias atomic-write="python /path/to/atomic_file.py"
```

---

## 快速开始

### 命令行使用

```bash
# 写入文本
atomic-write config.json --text '{"key": "value"}'

# 从另一个文件安全替换
atomic-write production.sql --file /tmp/new_schema.sql

# 从 stdin 读取
cat large_dump.json | atomic-write output.json --file -

# 严格模式（任何 fsync 失败都算错误）
atomic-write important.db --file new_data.bin --strict

# 不允许降级（目录 fsync 失败直接报错）
atomic-write data.txt --text "hello" --no-degraded
```

### 预检 / 预演

在实际写入前，先检测目标路径的崩溃安全能力：

```bash
# 只检查（不改文件）
atomic-write --check /path/to/config.json

# 预演（检查 + 模拟写入结果）
atomic-write --dry-run config.json
```

输出示例：

```
Crash-safety check for: /home/user/config.json
  Parent directory: /home/user
  Parent directory exists: YES
  Same filesystem: YES (rename will be atomic)
  Directory fsync: NOT SUPPORTED ([Errno 5] FlushFileBuffers failed ...)
  Crash-safety level: DEGRADED
  Missing guarantees: directory fsync not supported (directory entry may not persist after rename)
  WARNING: Directory fsync not supported: ...

Dry-run result: Write would succeed in DEGRADED mode (exit code 2)
```

### 退出码语义

| 退出码 | 含义 | 目标文件状态 | 崩溃安全承诺 |
|--------|------|-------------|-------------|
| 0 | 成功，全部 fsync 完成 | 已替换为新内容 | **完整**：断电也不会损坏 |
| 1 | 写入失败（rename 之前出错） | **未修改**，仍是旧内容 | 无（但旧内容完好） |
| 2 | 写入成功但降级 | 已替换为新内容 | **部分**：不半写，但断电可能回滚到旧内容 |
| 3 | 参数错误 | 未修改 | — |
| 4 | 部分完成（rename 已执行但目录 fsync 失败，strict/no-degraded 模式） | **已替换为新内容** | **缺失**：目录项未持久化，断电可能回滚 |

> ⚠️ 退出码 4 是一个特殊状态：内容已经替换成功了，但目录项的 fsync 失败。
> 此时 **不能** 说"旧文件不变"——文件确实已经是新内容，只是如果立即断电，目录项可能回滚。

### Python API 使用

```python
from atomic_file import atomic_write, atomic_write_read, check_crash_safety

# 预检
check = check_crash_safety('config.json')
print(f'Fully crash-safe: {check.would_be_fully_crash_safe}')
print(f'Dir fsync supported: {check.dir_fsync_supported}')
print(f'Same filesystem: {check.same_filesystem}')

# 写入
result = atomic_write('config.json', '{"key": "value"}')
print(f'Success: {result.success}')
print(f'Fully crash-safe: {result.fully_crash_safe}')
print(f'Rename done: {result.renamed}')
print(f'Temp fsync: {result.temp_file_fsync}')
print(f'Dir fsync: {result.dir_fsync}')

# 读取（便捷函数）
content = atomic_write_read('config.json')
```

**返回值 `AtomicWriteResult` 字段：**

```python
AtomicWriteResult(
    success: bool,                  # 目标文件是否已更新
    fully_crash_safe: bool,         # 是否完成全部 fsync 步骤
    temp_file_fsync: str,           # 临时文件 fsync 状态
    dir_fsync: str,                 # 父目录 fsync 状态
    renamed: bool,                  # rename 是否已完成（区分 rename 前后的失败）
    warnings: List[str],            # 降级/跨 FS 等警告信息
    target_path: Path,              # 写入的目标文件绝对路径
)
```

**预检结果 `CheckResult` 字段：**

```python
CheckResult(
    target_path: Path,              # 目标文件绝对路径
    parent_dir: Path,               # 父目录绝对路径
    parent_dir_exists: bool,        # 父目录是否存在
    same_filesystem: Optional[bool],# 同一文件系统？None=无法判断
    dir_fsync_supported: Optional[bool], # 父目录 fsync 支持？None=未测试
    dir_fsync_error: Optional[str], # fsync 失败时的错误信息
    would_be_fully_crash_safe: bool,# 能否达到完全崩溃安全
    warnings: List[str],            # 警告信息
)
```

---

## 常见场景选择指南

不同场景对崩溃安全的承诺等级要求不同。下表帮你选择正确的调用方式。

### 场景与承诺等级

| 场景 | 风险容忍度 | 推荐调用 | 能承诺什么 |
|------|-----------|---------|-----------|
| **配置文件更新** | 不能丢配置，也不能损坏 | `atomic-write config.json --file new.json` | ✅ 不半写 ✅ 断电后目录项落盘（如果 fsync 支持） |
| **小数据库快照** | 一个快照都不能丢 | `atomic-write snapshot.db --file tmp.db --strict` | ✅ 不半写 ✅ 要么完整要么报错（退出码 4 = 快照已写入但未 100% 落盘） |
| **日志轮转** | 丢一条日志可接受 | `atomic-write current.log --file rotated.log` | ✅ 不半写（日志不会损坏） ⚠️ 降级模式下断电可能丢最后一条 |
| **跨分区写文件** | 必须 atomic | — | ❌ 跨文件系统 rename 非原子，**无法保证**，应先写同分区再移动 |
| **网络驱动器** | 取决于协议 | `atomic-write file.txt --text "data" --check 先测` | ⚠️ SMB/NFS 的 fsync 语义取决于服务端实现 |

### 各场景详细说明

#### 1. 配置文件更新

配置文件通常较小、更新不频繁，但**绝对不能损坏**——损坏意味着服务无法启动。

```bash
# 标准方式（推荐）
atomic-write /etc/myapp/config.json --file /tmp/new_config.json

# Python
result = atomic_write('/etc/myapp/config.json', new_config_text)
if result.is_degraded:
    # 记录日志，但不必 panic——配置已经写入了，只是断电可能回滚
    log.warning(f'Config written in degraded mode: {result.warnings}')
```

**承诺等级**：
- Linux ext4/xfs 上：**完整崩溃安全**（退出码 0）
- Windows NTFS 上：通常是**降级**（退出码 2），因为目录 fsync 可能不支持
- 降级模式下：配置不会半写损坏，但如果在写入后几秒内断电，可能回滚到旧配置

#### 2. 小数据库快照

数据库快照是定期保存的全量状态，**宁可报错也不能写入半截数据**。

```bash
# 严格模式：任何 fsync 失败都算错误
atomic-write /data/snapshot.db --file /tmp/snapshot.tmp --strict
```

```python
try:
    result = atomic_write('/data/snapshot.db', snapshot_data, strict=True)
except OSError as e:
    # 两种情况：
    # 1. rename 前失败：旧快照完好，可以重试
    # 2. rename 后目录 fsync 失败（退出码 4）：
    #    新快照已写入，但目录项可能未落盘
    if result.renamed:
        # 快照数据确实已经替换成功了
        # 只是断电保护不完全
        log.error(f'Snapshot written but dir fsync failed: {e}')
    else:
        # 旧快照完好，可以安全重试
        log.error(f'Snapshot write failed, old data intact: {e}')
```

**承诺等级**：
- 退出码 0：**完整崩溃安全**，快照断电不丢
- 退出码 1：旧快照完好，可安全重试
- 退出码 4：快照**已替换**为新内容，但目录项 fsync 缺失，断电可能回滚到旧快照

#### 3. 日志轮转

日志轮转的核心需求是**日志文件不损坏**，丢最后几条日志可以接受。

```bash
# 默认模式即可——不半写最重要，降级可接受
atomic-write /var/log/app/current.log --file /var/log/app/rotated.log
```

**承诺等级**：
- ✅ **不半写**：即使降级模式，rename 仍然原子，日志文件不会损坏
- ⚠️ **降级风险**：如果目录 fsync 不支持，断电后最后一条日志的轮转可能丢失
- 日志场景下这个风险完全可接受

#### 4. 跨分区场景

如果临时文件和目标文件在不同分区/挂载点，**rename 不是原子的**，本工具无法保证崩溃安全。

```bash
# 先检查
atomic-write --check /other_partition/data.txt
# 输出: Same filesystem: NO (rename will be copy+unlink, NOT atomic!)

# 正确做法：先写到同分区，再用 mv 移动
atomic-write /tmp/data.txt --file new_data.bin
mv /tmp/data.txt /other_partition/data.txt
```

#### 5. 网络驱动器（SMB/NFS）

```bash
# 先预检，看服务端是否支持 fsync
atomic-write --check /mnt/nfs_share/data.txt

# NFS: 通常支持 fsync，但取决于服务端 mount 选项（sync vs async）
# SMB/CIFS: 取决于服务端实现，Windows SMB 通常支持
```

**承诺等级**：
- NFS `sync` 模式：通常**完整崩溃安全**
- NFS `async` 模式：**降级**（服务端可能缓存写入）
- SMB：取决于服务端，用 `--check` 预检

---

## 核心原理详解

### 问题：为什么直接覆盖写不安全？

考虑 `open("f", "w").write(NEW_BYTES)`：

```
应用 write() → 内核页缓存(Page Cache) → pdflush 异步刷盘 → 磁盘控制器缓存 → 物理介质
```

在中间**任意一步断电**，文件可能处于：
- 大小为 0（truncate 完成但数据未写）
- 半截（写了前 50KB 后面没写）
- 某些块是新数据某些块是旧数据

**文件既不是旧内容也不是新内容——损坏。**

---

### 解决方案：四步原子写入协议

```
时间轴 ──────────────────────────────────────────────────────────────────────►
  │
  ① mkstemp(dir=父目录) ── 在目标文件**同一目录**创建临时文件
  │                         ★ 必须同一目录 → 同一文件系统 → rename 才能原子
  │
  ② write(全部数据) ─────── 把新内容写入临时文件
  │
  ③ fsync(临时文件 fd) ─── 强制：数据块 + inode 元数据（大小、mtime）刷到物理介质
  │
  ④ close(临时文件) ─────── 关闭 fd
  │
  ⑤ os.replace(tmp, target)  同一文件系统内的 rename() 系统调用
  │
  ⑥ fsync(父目录 fd) ───── 对父目录执行 fsync，确保目录项持久化
  │
  ⑦ finally 清理 ───────── 任一步骤失败则删除临时文件
```

---

### 关键问题 1：为什么同一文件系统内的 rename 是原子的？

**POSIX SUSv4 标准明确规定：**

> "If `rename()` fails, neither old nor new shall be modified in any observable way."
> "The `rename()` function shall be atomic with respect to the visibility of the resulting directory entry."

**实现层面（以 ext4/xfs 为例）：**

rename 只修改**目录项中的文件名→inode 指针**，不移动任何数据块：

1. 取得父目录的 inode 锁
2. 在同一个 **日志事务（jbd2）** 内做三件事：
   - 删除旧目录项（如果 target 已存在）
   - 增加新目录项（`tmp` → inode）
   - 旧 target 的 inode 硬链接数减一，tmp inode 不变
3. 事务要么整体 commit，要么整体回滚
4. 对观察者：任何时刻要么看到旧文件，要么看到新文件，不存在中间状态

**⚠️ 关键前提：源和目标必须在同一挂载点（同一文件系统）。**

如果跨文件系统调用 rename，Linux 内核会**静默地将其退化成 `copy + unlink`**：
- 先把 tmp 文件的所有数据块复制到 target 所在文件系统
- 再删除 tmp
- 这个过程**完全不是原子的**，中途崩溃会留下损坏的 target

本工具通过 `_verify_same_filesystem()` 检测跨 FS 情况并发出警告。

---

### 关键问题 2：为什么仅仅 rename 还不够，必须先 fsync 临时文件？

```
write(tmp) → [此处崩溃] → rename → fsync(dir)
```

**崩溃时刻 A：`write(tmp)` 完成但 `fsync(tmp)` 还没做，此时断电。**

- `rename` 已经完成了 → 目标文件指向 tmp 的 inode
- 但 tmp 的数据块还在 Page Cache 里，根本没刷到磁盘
- 重启后：inode 存在，但它指向的数据块是旧垃圾（或者全零）
- **结果：目标文件存在，但内容是损坏的**

**`fsync(tmp)` 防御的就是这个崩溃时刻。** 它确保在 rename 之前，临时文件的所有数据和元数据（大小、权限）都已持久化到物理介质。

---

### 关键问题 3：为什么 rename 之后还要 fsync 父目录？

```
write(tmp) → fsync(tmp) → rename → [此处崩溃] → fsync(dir)
```

**崩溃时刻 B：`rename` 系统调用返回到用户态，但 `fsync(dir)` 还没做，此时断电。**

rename 操作修改了父目录的数据块（目录项），但这个修改也只是在 Page Cache 里，没刷到磁盘。

重启后可能发生：

| 场景 | 后果 |
|------|------|
| target 之前不存在（新建文件） | 目录里找不到 target → **看起来什么都没写过**。数据静静地躺在一个没有目录项指向的孤儿 inode 里 |
| target 之前存在（覆盖写） | 目录项仍然指向**旧** inode → **应用读到旧内容**，以为写成功了但其实丢失了这次更新 |

**`fsync(dir)` 防御的就是这个崩溃时刻。** 它确保目录项的修改（rename 产生的）持久化到磁盘。

> **经典踩坑案例：** RethinkDB、早期 SQLite、Redis AOF 都曾因为漏掉目录 fsync 导致崩溃后数据"消失"。

---

### 各步骤防御崩溃时刻汇总表

| 步骤 | 操作 | 防御的崩溃时刻 | 漏掉后的最坏后果 |
|------|------|----------------|------------------|
| ① | mkstemp(dir=parent_dir) | — | 跨文件系统 → rename 变成 copy+unlink **非原子**，中途崩溃损坏文件 |
| ② | write(数据) | — | （这一步崩溃不影响旧文件，只留下半写的临时文件，会被 finally 清理） |
| ③ | **fsync(临时文件)** | ② 与 ⑤ 之间断电 | inode 指向的块是旧垃圾/零 → rename 后目标文件**内容损坏**（inode 对但数据块错） |
| ④ | close(fd) | — | （多数 FS 不严格依赖，但某些网络 FS 需要 close 才释放锁） |
| ⑤ | rename(tmp, target) | ⑤ 执行中途断电 | **rename 本身保证原子性**：要么全完成要么全不做，无中间状态 |
| ⑥ | **fsync(父目录)** | ⑤ 与 ⑦ 之间断电 | 目录项回滚到旧状态 → 新建文件"消失"、覆盖写**回滚到旧内容**（最隐蔽的 bug） |
| ⑦ | finally unlink(tmp) | 任一步骤后正常异常 | 临时文件泄漏占用磁盘空间 |

---

## 关于"降级模式"（Degraded Mode）

### 什么时候会降级？

降级发生在 **rename 成功但父目录 fsync 失败** 时。常见原因：

- Windows 下 `FlushFileBuffers` 对某些文件系统/目录返回错误
- FAT32、exFAT 不支持目录 fsync
- SMB 网络共享的服务端不支持
- NFS async 模式

### 降级模式下的承诺

降级模式**不是什么都没做**，它仍然提供了重要的保证：

| 承诺 | 完整模式（退出码 0） | 降级模式（退出码 2） |
|------|---------------------|---------------------|
| 不会出现半写损坏 | ✅ 是 | ✅ 是 |
| 数据已完整替换 | ✅ 是 | ✅ 是 |
| 断电后目录项落盘 | ✅ 是 | ❌ 不能保证（可能回滚到旧内容） |

### 本工具的处理策略

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| **默认 `allow_degraded=True`** | 返回 `fully_crash_safe=False`，退出码 2 | 大多数应用：写入成功比完美保证更重要 |
| **`strict=True`** | 抛出 `OSError`，退出码 4 | 最严格场景：注意此时文件**已替换**，不是旧内容 |
| **`--no-degraded`** | 同 strict，退出码 4 | 同上 |

### strict/no-degraded 模式下目录 fsync 失败的准确含义

这是最容易误解的点：

```
write(tmp) → fsync(tmp) ✅ → rename ✅ → fsync(dir) ❌
```

此时：
1. **目标文件已被替换为新内容**（rename 已完成）
2. 新内容的数据块已持久化到磁盘（fsync(tmp) 已完成）
3. **缺失**：目录项的修改未持久化（fsync(dir) 失败）
4. **风险**：如果**现在立即断电**，重启后目录项可能回滚，文件可能变回旧内容
5. **如果不断电**：操作系统最终会把目录项刷盘，之后就和完整模式一样安全

所以退出码 4 ≠ "写入失败" ≠ "旧文件不变"。它的准确含义是：
**写入已成功，数据已在磁盘上，但崩溃安全的最后一步（目录项持久化）缺失，需要几秒钟让操作系统自动完成刷盘后才安全。**

---

## 真实世界中的应用

这套协议是工业界的标准做法，被用于：

- **SQLite**：WAL 模式下的 checkpoint、master journal 写入
- **LevelDB / RocksDB**：MANIFEST 文件更新、CURRENT 指针切换
- **Redis**：AOF 和 RDB 的持久化切换
- **ZooKeeper**：事务日志的原子更新
- **大多数配置管理工具**：`etckeeper`、`consul` 等的配置文件写入

---

## API 参考

### `atomic_write()`

```python
atomic_write(
    path: Union[str, Path],
    data: Union[bytes, str],
    encoding: Optional[str] = None,
    permissions: Optional[int] = None,
    temp_suffix: str = '.tmp',
    temp_prefix: str = '.~',
    allow_degraded: bool = True,
    strict: bool = False,
) -> AtomicWriteResult
```

### `check_crash_safety()`

```python
check_crash_safety(
    target: Union[str, Path],
) -> CheckResult
```

### `atomic_write_read()`

```python
atomic_write_read(
    path: Union[str, Path],
    read_encoding: Optional[str] = 'utf-8',
) -> Union[bytes, str]
```

### 命令行参数

```
atomic-write <target>
    [--text CONTENT | --file SOURCE | --check | --dry-run]
    [--encoding ENC]
    [--binary]
    [--permissions OCTAL]
    [--strict]
    [--no-degraded]
    [--quiet]
```

---

## 测试

```bash
python test_atomic_file.py -v
```

---

## 常见误区

1. ❌ **"rename 已经原子了，不需要 fsync"**
   → 漏掉 fsync(tmp) 会导致内容损坏

2. ❌ **"fsync 了文件就够了，目录不用"**
   → 漏掉 fsync(dir) 会导致更新"消失"

3. ❌ **"strict 模式失败 = 旧文件不变"**
   → 目录 fsync 失败时 rename 已经完成，**文件已替换为新内容**

4. ❌ **"跨文件系统 rename 也原子吧？内核应该会处理"**
   → 不会。跨 FS rename 退化成 copy+unlink，完全非原子

5. ❌ **"Windows 下不用关心这个，NTFS 是事务性的"**
   → NTFS 的元数据事务性只保证 rename 本身原子，但不保证 fsync 的语义，仍然需要显式刷盘

6. ❌ **"降级模式 = 写入失败"**
   → 降级模式下数据已完整写入，只是断电保护不如完整模式
