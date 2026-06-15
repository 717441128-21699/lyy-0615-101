# 崩溃安全的原子文件写入工具

一个 Python 实现的生产级原子文件写入工具，解决"直接覆盖写 + 中途断电 = 损坏文件"的经典问题。

采用标准的 **"写临时文件 → fsync → 原子 rename → 父目录 fsync"** 四步协议，确保在任意时刻崩溃，目标文件要么是完整的旧内容，要么是完整的新内容，绝不会处于半写的损坏状态。

---

## 快速开始

### 命令行使用

```bash
# 写入文本
python -m atomic_file config.json --text '{"key": "value"}'

# 从另一个文件安全替换
python -m atomic_file production.sql --file /tmp/new_schema.sql

# 从 stdin 读取
cat large_dump.json | python -m atomic_file output.json --file -

# 严格模式（任何 fsync 失败都算错误）
python -m atomic_file important.db --file new_data.bin --strict

# 不允许降级（目录 fsync 失败直接报错）
python -m atomic_file data.txt --text "hello" --no-degraded
```

**退出码语义：**

| 退出码 | 含义 |
|--------|------|
| 0 | 成功，且完成全部 fsync 步骤，提供完整崩溃安全承诺 |
| 1 | 写入失败，**目标文件保持不变**（仍是旧内容） |
| 2 | 写入成功但降级模式，部分 fsync 步骤失败，崩溃安全不完整 |
| 3 | 参数错误 |

### Python API 使用

```python
from atomic_file import atomic_write, atomic_write_read

# 写入
result = atomic_write('config.json', '{"key": "value"}')
print(f'Success: {result.success}')
print(f'Fully crash-safe: {result.fully_crash_safe}')
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
    warnings: List[str],            # 降级/跨 FS 等警告信息
    target_path: Path,              # 写入的目标文件绝对路径
)
```

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
  │   (os.fsync(f.fileno()))
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
| target 之前不存在（新建文件） | 目录里找不到 target → **看起来什么都没写过**。数据静静地躺在一个没有目录项指向的孤儿 inode 里（lost+found 可找回，但应用完全无感知） |
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

### 问题：Windows 下的目录 fsync

在 Windows 上，Python 的 `os.fsync()` 不支持目录句柄。正确做法是通过 Win32 API `CreateFileW` 打开目录，再调用 `FlushFileBuffers`。

但并非所有 Windows 文件系统都支持 `FlushFileBuffers` 对目录的操作：
- NTFS 通常支持
- FAT32、exFAT 可能不支持
- 某些 SMB 网络共享可能失败

### 本工具的处理策略

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| **默认 `allow_degraded=True`** | 目录 fsync 失败时，返回 `AtomicWriteResult(fully_crash_safe=False)`，退出码 2，打印明确警告 | 大多数应用，希望"即使降级也不丢数据" |
| **`strict=True`** | 任何 fsync 失败直接抛出 `OSError`，退出码 1 | 最严格的场景，宁失败不降级 |
| **`allow_degraded=False`** | 目录 fsync 失败抛出异常，但临时文件 fsync 失败不影响（因为 rename 前就抛了） | 中间态 |

**降级模式下的崩溃风险：**
- rename 本身仍然是原子的（只要同 FS）
- 仍然不会出现半写的损坏文件
- **但**：如果在调用返回后立即断电，有小概率回滚到旧内容
- 这是在"无法完成目录 fsync"的前提下所能做到的最佳努力（Best Effort）

降级模式下 `warnings` 字段会包含详细说明，命令行也会明确标出 `DEGRADED MODE`。

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
    [--text CONTENT | --file SOURCE]
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

测试覆盖：
- 基础读写（bytes/str）
- 覆盖写旧文件保留
- 失败回滚（临时文件清理、旧文件不变）
- 并发写入原子性
- 大文件写入
- 编码支持
- 命令行入口
- 降级模式行为
- 目录 fsync 失败时不假装成功

---

## 常见误区

1. ❌ **"rename 已经原子了，不需要 fsync"**
   → 漏掉 fsync(tmp) 会导致内容损坏

2. ❌ **"fsync 了文件就够了，目录不用"**
   → 漏掉 fsync(dir) 会导致更新"消失"

3. ❌ **"我用了 tmpfs /dev/shm，崩溃安全没问题"**
   → tmpfs 本身就不是持久化的，谈崩溃安全没有意义

4. ❌ **"跨文件系统 rename 也原子吧？内核应该会处理"**
   → 不会。跨 FS rename 退化成 copy+unlink，完全非原子

5. ❌ **"Windows 下不用关心这个，NTFS 是事务性的"**
   → NTFS 的元数据事务性只保证 rename 本身原子，但不保证 fsync 的语义，仍然需要显式刷盘
