# cad-floorplan-decompose

试用版 **v0.1**。把一张住宅平面（DWG/DXF）按墙体、窗、门、柜体、洁具、电器、家具、灯具等类别一次拆开，把拿不准的集中成 Q 清单，再用一屏审核图确认，最后合并为每类一色一层的总图。

只做平面分类、审核与合并，不含三维建模。源文件始终只读，不要上传到在线转换站。

## 流程

1. **全分类 + Q 清单**：本地把 DWG 转成 DXF，按规则对可见实体初分类，列出未归类项和待确认项（同名块合并问一次，候选通常 1–2 个）。
2. **一屏审核**：总览按类着色并编号，旁边是各类缩略图；只改被圈出的类别。
3. **终审合并**：把确认结果写回规则里的 `learned` 后重跑，再合并总图。建议补充的内容（如加灯）必须另层另色，并标明「非原图」。

也可改成一类一类看，硬性规则相同。完整约定见 [SKILL.md](SKILL.md)，默认规则种子见 [rules.yaml](rules.yaml)。

## 类别与合并配色

| 图层 | 颜色 |
| --- | --- |
| 墙体_保留 | `#E6E6E6` |
| 窗 | `#FFBF00` |
| 墙体_门框 | `#7EB8FF` |
| 柜体 | `#FC1491` |
| 家具 | `#B266FF` |
| 洁具 | `#00E5FF` |
| 电器 | `#FF8C00` |
| 门 | `#39FF14` |
| 灯具 | `#FF3B3B` |
| 灯具_建议补充（非原图） | `#FF6EC7` |
| 其他（未归类） | `#707070` |

审核图为黑底；底图中灰，待确认用金色 Q 编号。人物图块和尺寸标注不进入合并。

## 安装

需要 Python 3 与 Node.js。

```bash
python3 -m pip install -r requirements.txt
npm install
```

中文标注请自行准备 CJK 字体（例如 Noto Sans CJK SC），用 `--font` 指向字体文件。

`scripts/dwg2dxf.mjs` 按以下顺序查找 `@mlightcad/libredwg-web`：

1. 环境变量 `LIBREDWG_WEB`（指向该包的目录）
2. 仓库或当前目录下的 `node_modules/@mlightcad/libredwg-web`

在仓库根目录执行 `npm install` 即可。若包安装在别处：

```bash
export LIBREDWG_WEB=/path/to/node_modules/@mlightcad/libredwg-web
```

## 快速开始

`SKILL.md` 示例里的 `S=` 是一份本机技能目录路径，请改成你的克隆路径：

```bash
S=/path/to/cad-floorplan-decompose

# 源 DWG 只读，输出写到本地工作目录（*.dwg / *.dxf 默认不纳入版本库）
mkdir -p project out
node $S/scripts/dwg2dxf.mjs source.dwg project/source.dxf
cp $S/rules.yaml project/rules.yaml

# 视口按源文件坐标填写（xmin,ymin,xmax,ymax）；非毫米单位会自动换算到工作副本
python3 $S/scripts/classify.py --source project/source.dxf --rules project/rules.yaml \
    --viewport xmin,ymin,xmax,ymax --out-dir out/class1 --q-crops --font /path/to/NotoSansCJKSC.otf

# 一屏审核（读取 units.json 中的工作副本和换算后视口）
python3 $S/scripts/compose_review.py --units out/class1/units.json \
    --classification out/class1/classification.json --questions out/class1/questions.json \
    --font /path/to/NotoSansCJKSC.otf --out out/class1/review.png \
    --title '住宅平面初分类 第1轮审核'
```

答完 Q 后，把块名归属写入 `project/rules.yaml` 的 `learned`，再对 `out/class2` 重跑分类。单类出图和总图合并见 `SKILL.md` 中的 `render_review.py`、`dxftools.py`、`merge_master.py` 示例。

## 目录

| 路径 | 说明 |
| --- | --- |
| `SKILL.md` | 流程、规则约定与命令示例 |
| `rules.yaml` | 分类规则种子；用户确认写入 `learned` |
| `scripts/classify.py` | 初分类、Q 清单、单位换算 |
| `scripts/compose_review.py` | 一屏按类着色总览 |
| `scripts/render_review.py` | 单类审核图 |
| `scripts/dxftools.py` | 审计、按句柄导入、重叠检查 |
| `scripts/merge_master.py` | 合并总图 |
| `scripts/dwg2dxf.mjs` | 本地 DWG→DXF |
| `requirements.txt` | Python 依赖 |
| `package.json` | Node 依赖（仅转换用） |

## 当前局限

- 试用版 v0.1，规则和阈值还在迭代。
- 自测只在一张内部住宅平面和一张公开样图上跑过，换图时请先核对审核图，不要假设召回率。
- 不含三维建模、渲染或施工图出图。
- 种子规则面向常见中英文图层/块名和毫米尺度的住宅平面；少见图层或非标准画法仍会进入「其他」或 Q 清单。
