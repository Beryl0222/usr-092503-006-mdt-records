# 肿瘤多学科决策记录服务

本仓库保存院内多学科讨论使用的基础角色、材料类别和确认状态。所有标识用于记录工作流，不表达诊断或治疗建议；患者信息在进入系统前应完成去标识化处理。

## 校验方式

执行测试：

```bash
python3 -m unittest discover -s tests -v
```

执行构建检查：

```bash
python3 -m compileall -q src
```
