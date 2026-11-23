# 旧城街巷修缮分期的经营影响与缓冲资格

本地数据与术语材料供业务人员整理分析资料使用，所有记录均为合成样例。

## 目录

`fixtures/` 保存合成输入，`docs/` 说明字段与业务规程，`tests/` 检查文件结构和业务规则，
`street_buffer/` 提供估计、审核与方案比较工具，`examples/` 是更完整的合成账册。

## 环境和测试

Python 3.11 标准库，无需第三方依赖。运行 `python3 -m unittest discover -s tests` 检查输入结构
和业务规则，运行 `python3 -m compileall -q .` 检查 Python 文件语法。

## 工具

```bash
python3 -m street_buffer estimate --data fixtures
python3 -m street_buffer review   --data fixtures --format text
python3 -m street_buffer merchant --data fixtures --shop S1
```

用法、估计口径与扩展输入见 `street_buffer/README.md`。
