# 言明通智能体应用
> Y.M.T. Agents
2026年9月25日版次0.1.0

## 源代码快速启动

环境：Python 3.14 + [uv](https://docs.astral.sh/uv/)。

```powershell
# 安装依赖（创建 .venv 并锁定 uv.lock）
uv sync

# 启动
uv run python -m app

# 或指定文件启动
python src/app/main.py
```

已激活虚拟环境时（`.venv\Scripts\Activate.ps1`），也可直接：

```powershell
python -m app

# 或指定文件启动
python src/app/main.py
```
## 项目说明
在常见Agent平台的基础上（以当今的Cowork类为指南），增加了自研思考系统和存储系统，除了角色提示词，其他内容全部都可以自选开启或关闭。

一般来说，整个项目都可以正常运行，目前暂时支持从源代码启动，以目前的版本，可能还有很多潜在问题，后续会逐步修缮。

可以只使用src里面的main.py在Python环境下启动，也可以等后续发行版的整合包做出来使用可执行文件启动。

项目目的是给出一个模块化分合思维的项目，可以全盘控制项目的启动模式，当然后续会改动成可以在对话内也可以控制。然后其他部分倒是不用多说，后续如果有问题会逐步修订。

## 其他

联合的自研系统：
1. [DPIM存储系统](https://github.com/BiHongCue876/dpim)
2. [BTCM思考链路](https://github.com/BiHongCue876/btcm)

总体设计：BiHongCue876

## 通过这个项目的希望与后续改良的思维
真正地通过这个项目，向广大AI享用者构建一个能方便应用并且不被偷代码的自由化GUI，做一个自定义的窗格应用。而且希望在功能多的情况下，保持不臃肿不浪费，不白费，有效应用tokens。允许依据每个用户自己的习惯，默认也好，导入使用也好，不要浪费资源，做自己想做的事情。