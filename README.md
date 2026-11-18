# container-layer-deduper

Python 容器镜像层去重库：将压缩 layer 解包为文件元数据，使用内容定义分块
（content-defined chunking）生成可跨层共享的 chunk，并提供标签引用与引用
计数式垃圾回收。

## 工作流程

1. `push_layer(blob)`：接受 gzip 压缩或裸 tar layer，解包后归一化为文件元
   数据（路径排序、丢弃 mtime/uid/gid、重复路径含 `.wh.*` 白名单文件取最后
   一条），再对每个文件内容做 FastCDC 分块，chunk 以 SHA-256 内容寻址存储。
2. `put_manifest(manifest)` / `tag(name, digest)`：登记 manifest 并打标签。
3. `pull_layer(digest)`：从共享 chunk 重建 tar，恢复文件内容与权限位。
4. `gc()`：以标签为根做标记-清除——先回收无标签引用的 manifest，再回收无
   manifest 引用的 layer，最后回收无 layer 引用的 chunk。删除标签后，仍被
   其他 manifest 引用的 chunk 不会被回收。

## 分块参数

`LayerStore(min_size, avg_size, max_size)`，默认 `4096 / 16384 / 65536` 字节：

- `min_size`：chunk 下限，小于该值不产生切点，避免碎块过多。
- `avg_size`：目标平均大小，建议取 2 的幂。滚动哈希（gear 表，splitmix64
  固定种子，跨进程确定性）的掩码由它推导：平均点之前用较严掩码
  `2^(bits+1)-1`，之后用较松掩码 `2^(bits-1)-1`（FastCDC 双掩码策略）。
- `max_size`：chunk 硬上限，超过即强制切分。

切点只取决于文件字节内容，与 tar 条目顺序、mtime、压缩算法/级别、重复
白名单条目无关，因此同一逻辑层重打包后 digest 不变、chunk 完全复用。

## 去重率计算

`stats()` 返回：

- `logical_bytes`：历次推送累计的未压缩文件字节数（未去重）。
- `physical_bytes`：实际存储的唯一 chunk 总字节数。
- `dedup_ratio = 1 - physical_bytes / logical_bytes`，越接近 1 去重越有效。

## 部分上传

`begin_upload()` → `upload_part(id, data)`（可多次）→ `complete_upload(id)`
或 `abort_upload(id)`；也可用 `has_chunk(digest)` / `put_chunk(data)` 做
chunk 级断点续传，已存在的 chunk 在 `push_layer` 时自动复用。

## 测试

```sh
python -m unittest
```

覆盖：层重打包（顺序/mtime）、重复白名单文件、压缩差异、部分上传、GC
引用回收、重建时内容与权限恢复。
