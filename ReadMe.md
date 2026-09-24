# 言明通超级智能体应用
> Y.M.T. Super Agents
2026年9月23日版次0.0.10

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


## 通过这个项目的希望与后续改良的思维
真正地通过这个项目，向广大人民构建一个能方便应用并且不被偷代码的自由化GUI，同国内外那些什么Pi、Aider什么一样，但通过GUI实现，做一个自定义的窗格，并且要好用一点。而且我希望在功能多的情况下，保持不臃肿不浪费，不白费，有效应用tokens。允许依据每个用户自己的习惯，默认也好，导入使用也好，不要浪费资源，做自己想做的事情。