# 微缩胶片扫描卷盘质检工作台

Flask + SQLite + Pillow 的本地质检工具，前端为原生 JavaScript（无构建步骤）。
功能包括帧序列修订、告警、补扫回填配准核对、帧边界拆分/合并，以及定稿前的**二次验收（抽查复核）**。
所有图像分析都在本机完成。

## 环境要求

- **Python 3.10 / 3.11 / 3.12**（64 位）
- 依赖：Flask、Pillow（其余为 Flask 传递依赖，见 `requirements.txt`）

> ⚠️ **Pillow 与解释器 ABI 必须匹配。** Pillow 含 C 扩展（`PIL/_imaging…so`），
> 随仓库附带的 `.pyuser/lib/python3.11/site-packages` 仅能在 **CPython 3.11** 下使用。
> 把它复制到 Python 3.12（或任何其它小版本）会出现
> `ImportError: _imaging C module ... ABI` / 导入 PIL 直接退出。
> 正确做法是在**目标 Python 环境内用 pip 安装**对应的 Pillow wheel（见下），不要跨版本复制。

## 快速开始（venv，推荐）

Linux / macOS：

```bash
cd microfilm_qc
python3.11 -m venv .venv                 # 也可用 3.10 / 3.12
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python app.py
```

Windows（PowerShell）：

```powershell
cd microfilm_qc
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
python app.py
```

启动后访问 <http://127.0.0.1:5000>。
首次打开点「载入演示卷盘」即可看到含各类缺陷样例的内置数据。

若系统 Python 没有 `pip`/`venv`（精简发行版常见），先安装发行版包：

```bash
# Debian/Ubuntu
sudo apt-get install python3.11-venv python3-pip
```

也可用官方引导：下载 <https://bootstrap.pypa.io/get-pip.py> 后
`python get-pip.py`，再 `pip install -r requirements.txt`。

### 离线环境

在**能联网、且 Python 小版本与目标机一致**的机器上下载 wheel：

```bash
python -m pip download -r requirements.txt -d wheels
# 拷贝整个 wheels/ 到目标机后
python -m pip install --no-index --find-links=wheels -r requirements.txt
```

`pip download` 会按目标解释器选择正确的 manylinux/macosx/win wheel，避免 ABI 问题。

## Docker（可选）

```bash
docker build -t microfilm-qc .
docker run --rm -p 5000:5000 -v "$PWD/data:/app/data" microfilm-qc
```

镜像在构建阶段按其中的 Python 版本安装匹配的 Pillow；数据库与帧图像通过
`/app/data` 卷持久化。可用环境变量 `MICROFILM_QC_HOST` / `MICROFILM_QC_PORT`
覆盖监听地址（容器默认 `0.0.0.0:5000`）。

## 运行测试

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'
# 前端（需要 Node）：
node tests/test_rescan_viewer.js
```

## 二次验收（抽查复核）要点

- 复核员按**数量或占比**设定方案，系统用**可复现种子**在首段/中段/末段分别随机抽样，
  并强制纳入曾经重拍、回填、拆分/合并、人工越过配准门槛的图像；去重后排除缺图占位。
- 抽样开始即**锁定图像版本**（版本行、文件 MD5、朝向）。进入匿名审片，逐项判定
  清晰度、裁边、朝向、污损、内容缺失；不合格必填备注，可转入重拍。
- 抽样后**旋转朝向**：旧记录保持 `void`（历史与作废缘由保留），同一轮自动产生一条
  按新朝向锁定的待判项；该项未完成五项判定前，本轮不能通过、定稿或移交。
- 失败数超过可配置门限即不通过，可**自动加抽**（同链新轮、可复现）或**退回整卷**；
  无新图可加抽时仍可退回，最终处置完整记录。
- 定稿必须关联**有效的通过结论**；移交 JSON/CSV 写入方案参数、种子、复核人、
  作废缘由与最终处置：`/api/reels/<id>/reviews/handoff.json|.csv`。

## 数据位置

数据库与图像默认在包目录下的 `data/`（`qc.db`、`frames/`、`rescan/`、
`boundary/`、`exports/`），首次运行自动创建。
