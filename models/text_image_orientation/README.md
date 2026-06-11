把 PaddleClas(PULC) 的 `text_image_orientation` 推理模型放在本目录。

需要至少包含：

- `inference.pdmodel`
- `inference.pdiparams`
- （可选）`inference.pdiparams.info`

放好后，项目会优先从 `<project_root>/models/text_image_orientation/` 加载模型，
不会再依赖 `~/.paddleclas/inference_model/...` 的自动下载缓存目录。

