---
name: cad-floorplan-decompose
description: 当用户要把住宅平面 DWG/DXF 按墙体、门、柜体、洁具、电器、家具、灯具等类别一次拆完、集中确认疑点并合并总图时使用；目标 2–3 轮完成，也可按用户要求改回逐类细看。
---

# 住宅平面拆解、审核与合并

把一张住宅平面（DWG/DXF）先看懂，再**一次分到全部类别**，把拿不准的集中成一份 Q 清单请用户一次答完，用「一张总览 + 各类小图」做一屏审核，定稿后合并为每类一色一层的总图。

步骤的本质与逐类拆解相同，但节奏改为批量，避免一类一类来回等确认。具体项目的坐标、句柄、房间名写在该项目的清单里，不要写进本技能。默认规则种子见同目录 `rules.yaml`。

**目标轮次（新图 2–3 轮）：**

1. 全分类 + Q 清单（用户一次回答疑点）
2. 一屏审核，只改被圈出的类别
3. 终审（通常只动细节或「其他」归类）

若用户明确要求「一类一类仔细看」，改用文末的**单类逐步模式**，硬性规则不变。

## 硬性规则

- **本地解析，不上传第三方。** 用本机工具把 DWG 转成 DXF 后，用 ezdxf 读。源文件不要送到在线转换站。
- **不改源 DWG。** 所有操作只读源文件；产出写到新的交付路径。
- **旧版不覆盖，版本递增命名。** `第N版` 定稿后，改动一律出 `第N+1版`。清单记下旧版时间戳，改完核对 mtime 未变。
- **不发明原图没有的几何。** 门扇弧线、灯具、墙垛等，原图没有就不要补画；建议补充的内容必须另层另色，并标明「非原图」。
- **能用规则判定的不要问。** 只有规则冲突、块名无法识别、或几何模棱两可时才进 Q 清单。
- **拿不准先问。** Q 项用金色编号，附小局部图和候选类别，等用户回答后再定类。

## 主流程（批量）

### 第 1 轮：一次全分类 + 集中问 Q

1. 本地把 DWG 转成 DXF，锁定本户视口。`--viewport` **按源文件坐标填写**（不要先手工缩放图纸）。`classify.py` 会读 `$INSUNITS`，源图不是毫米时只在输出目录写 `source-mm.dxf` 工作副本并按同一比例换算视口，**不改源文件**。先看懂户型、真正的入户、哪些是示意图或其它楼层，不要凭经验臆造。跳过不可见实体、Defpoints、关闭/冻结图层。尺寸标注与人物图块直接排除，不进分类。
2. 用 `rules.yaml` 对**所有可见实体**做初分类，同时得到：
   - 各类已归入的实体
   - **未归类清单**（从源头列出，不等到合并才发现遗漏）
   - **Q 清单**：把拿不准的实体汇总。同名块（或同形状的确认项）合并为一项。`classify.py --q-crops` 会把每项做成小局部图（灰底 #707070 + 金色高亮该块全部实例，图下注明块名、出现次数、外包尺寸、候选类别），拼成一张或少量几张 q-sheet，用户一眼能答。Q 只用于确认（候选应是 1–2 个），能用规则或形状判定的不要问。
3. 用户一次答完 Q。把每条回答写入 `rules.yaml` 的 `learned`（块名→类别，或例外），然后**重跑初分类**，再进入审核。不要把已能判定的问题再问一遍。

### 第 2 轮：一屏审核

给用户看尽量少的图。用 `compose_review.py` 把总览和各类缩略拼成**一屏审核图**（不要一次交十张大图）：

- **总览**：每类一色，「其他」中灰，已定类编号用黄圈，待确认用金色 Q 号
- **右侧/下方各类小图**：灰底只高亮本类

用户只圈有问题的类别或位置，其余直接通过。改稿只出**受影响类别的新版**和**合并新版**，未圈的类别不要重出。对照图只做被改区域的前后对比。

### 第 3 轮：终审

用户确认总览。若只改「其他」的归类或个别编号，再出合并新版，并同步受影响的单类新版。建议补充灯（若需要）在此轮按灯具规则补，必须另层另色、标明「非原图」。

## 规则文件

路径：技能目录下的 `rules.yaml`（可用 `--rules` 指向项目自己的副本）。**第一张图用种子规则；每张图、每次用户回答都要写回规则，供下一张图自动套用。**

### 格式要点

- `layer_aliases` / `block_keywords`：图层名、块名的通用别名与关键词（忽略大小写，子串匹配）。
- `rules`：自上而下匹配。`confidence: high` 命中即采纳；两条 high 指向不同类别则该实体进 Q。没有 high 时，仅一条 `medium` 则采纳，多条冲突则进 Q。全不匹配 → 未归类（合并进「其他」）。
- `match` 常用字段：`types`、`layers`、`layer_of`、`linetype_contains`、`colors`（ACI，256=BYLAYER）、`block_keywords_of`、`closed`、`min/max_thickness`（闭合框短边，毫米）、`min/max_area`、`min/max_extent`、`circle_r_min/max`、`not_layers`、`not_linetype_contains`。
- `learned.blocks`：用户确认过的**块名 → 类别**（优先于关键词）。
- `learned.exceptions`：尽量少用；仅当必须针对单个实体时才写句柄。

### 如何更新

用户回答 Q「块 `zb` 是马桶」之后，追加：

```yaml
learned:
  blocks:
    - {name: "zb", category: 洁具, note: "马桶"}
```

若回答「这批墙层黄线不是窗」，不要写死句柄作一般规则，应收紧或关闭对应种子规则（例如 `window-yellow-on-wall`），并在 note 里写原因。下一张图先跑更新后的规则，再生成新的未归类与 Q（已学会的块不应再出现在 Q 里）。

### 种子规则（通用表述，详见 rules.yaml）

这些是本技能从住宅平面实践中沉淀的默认判断，不含具体项目句柄：

- 灯具专用层上的实体归灯具；窗专用层归窗。
- **块名优先于图层**：块名含窗关键词（window / 窗 / chuang / glaz 等）直接归窗，即使插在非窗图层上。
- 墙层上的黄色（ACI 2）线段默认是窗线，与墙分色。
- 细密虚线（ISO10）默认是门扇/五金，归门。
- 拆除/拆砌图层不进保留墙体，进「其他」。
- 墙层虚线多为洞口或拆砌，不进保留墙体。
- 新建墙上、短边约 100–280mm 的墙厚闭合框归墙体；短边明显小于墙厚、整体又不大的框/短肢按**独立门垛归门**。连续墙轮廓上的洞口侧壁仍留墙体，不要当成门垛。
- 管道层上的圆（常见地漏）归洁具；管线路由（多段线）进「其他」。
- 天花层上的椭圆/样条多为灯槽，归灯具。
- 家具层上较大的虚线闭合框（短边较大、面积不太大）倾向柜内分割，归柜体；特别大的虚线框倾向地毯，归家具。
- 块名含「床/沙发/椅/桌/餐/窗帘」「马桶/台盆/花洒」「冰箱/灶/燃气/洗衣机/电视」「灯」「门」「窗」「柜」等中英文关键词时直接归类；看不出的块先走形状识别，再决定定类或进 Q。
- 未知块进 Q 时，候选**最多 2 个**（按形状得分取前两名），不要把全部类别都列上。
- 人物图块、尺寸标注排除，不进合并。
- 墙图层上的实线（**不限颜色**）默认进墙体；虚线、文字、尺寸仍排除。可能多收洞口细部或饰面线，一屏审核墙体时核对。

### 形状识别（不靠块名）

块名经常是乱码，不能作为通用规则。`classify.py` 在图层/线型规则之后，用 `rules.yaml` 里的 `shape` 阈值（毫米，住宅常见尺度）看几何：

- **图块（INSERT）**：统计虚拟几何。以样条为主 → 人物（排除）；大量微小圆点 → 花洒；坐便器尺度且多段圆弧、无填充 → 马桶；细长条且大量圆弧 → 窗帘；接近双人床尺度且线条较多 → 床；长条且大量圆弧 → 沙发；近方形、多圆弧、无大圆 → 椅凳；深×长接近冰箱且有较大圆 → 电器。不够把握的（如带填充的灶具尺度、细长无圆弧条）进 Q，只给 1–2 个候选请用户确认。
- **炸开的线**：先找种子再吸收紧邻细部，不要把整房间连成一块。带圆角的台盆框 + 旁边小椭圆/小圆（龙头、下水）；墙/管道层同心圆（地漏）；家具层较大同心圆 + 穿过圆心的短十字（筒灯）；内外相套、间距约板厚的柜框（含贴墙未闭合框），框内交叉线或等距分隔一经认定即归入该柜，但已识别的台盆/冰箱/电视等优先保留；沿墙、间距约柜深的 L 形外沿线只定这条线，不向框内扩散；床侧 400–600mm 近方形框作床头柜。细长窗帘盒；较大无圆角矩形桌面；天花层大虚线圆角框（灯槽）。只有柜深、缺少内框或交叉线的细长闭合框，作为确认项进 Q（柜体/家具）。
- 阈值写在 `shape` 下，不要写项目句柄或块名。高置信直接归类；中置信只进 Q。已由前面高/中规则定类的实体不再改。

## 归类边界（与种子规则一致）

与用户口头决定冲突时，以用户为准并写进规则与该版清单。

**墙体：** 承重/隔墙、新建墙、门框墙；**全部窗线**（窗与墙分色分层）。虚线拆砌不画成保留墙。连续洞口侧壁留墙体。不纳入门扇、独立门垛、柜体。

**门：** 门扇、五金、拉手、**独立门垛**。原图没有开启弧不要编造。入户按真实洞口认，不要被示意图或其它洞口带偏。

**柜体：** 成品/定制柜、衣柜、储物柜、电视柜、**床头柜**。不纳入洗手盆、灶具、冰箱、电视、桌子。

**洁具：** 马桶、浴缸、台盆的**盆和龙头**、地漏、淋浴花洒、淋浴玻璃。不要把整张台面轮廓算进洁具（岛台台面等归家具或柜，按用户决定）。

**电器：** 灶具、冰箱、洗衣机、电视、空调室内机等。禁止与其它类共用同一源句柄。

**家具：** 床（含床头板）、桌椅、沙发茶几、凳、窗帘、**窗帘盒/轨**、背景墙、地毯、岛台（含台面轮廓）、化妆桌。不纳入人物、小摆件/绿植、晾衣杆，以及已归灯具或柜体的实体。

**灯具：** 单独一类。原图灯、灯槽、天花虚线灯框。建议补充灯另色另层，编号区分，图例写「非原图」。少而精、能对称则对称、不压家具与洁具；**入户内侧（玄关）要有灯**，先确认真正入户门再放。

**合并时排除：** 人物、DIMENSION；清单注明数量。

## 审核图规范

- 黑底；**全图灰底 #707070**，只高亮当前类（一类一色）。总览图则每类一色，「其他」为中灰。
- **黄标签**编号（F01、D07…）；待确认用**金色 Q 编号**，用户回答并重跑后 Q 应消失或改成正式编号。
- 无网格、无填充（只描边）；不要给家具/柜体外接矩形框。
- 标题和图例写清版本、颜色、数量；建议灯写「非原图」。
- 视口锁定在本户范围内，后续版本才能复用标签坐标。
- 中文字体用 NotoSansCJKSC（或项目指定的 CJK 字体）。
- 交付前 `ezdxf audit` 为 0；用 Read 打开 PNG 目视核对，不要只看脚本 “saved”。

一屏审核的拼图要让用户在一屏内看到总览和各类缩略，而不是十张分开的大图。被圈改的类别再出清细节与局部对照。

## 合并规则

- 只从各类正式图层收集实体，不要把某类文件里残留的其它层再导入一遍。
- 每类一色一层；**颜色冲突要换**（十六进制相同，或视觉过近难以分辨）。墙体/窗/门框若与洁具/电器/门撞色，在合并图改色并在清单说明；单类小图可用该类习惯色。
- 用**源句柄**做跨类重复检查，必须零重叠。导入后交付 DXF 的 handle 会被重映射，去重和清单都用源句柄。
- 未归类可见几何进「其他」，中灰（如 #707070），与墙体浅灰明显区分；不加编号；图例加「其他（未归类，灰）」。把「其他」画在已分类层之下。
- 人物、DIMENSION 不放，清单注明。

## 工具链与常见坑

- **DWG→DXF：** 用 `scripts/dwg2dxf.mjs`（`LibreDwg.create` + `dwg_write_dxf`）本地导出。脚本**不依赖当前工作目录**，按下列顺序找 `@mlightcad/libredwg-web`：环境变量 `LIBREDWG_WEB`、`/workspace/cad-skill-pilot/node_modules/@mlightcad/libredwg-web`、脚本或 cwd 下的 `node_modules`。不要改源 DWG，不要把源文件上传到在线转换站。
- **单位：** 分类阈值按**毫米**。`classify.py` 读取 `$INSUNITS`（1 英寸×25.4、2 英尺×304.8、4 毫米、5 厘米×10、6 米×1000）；为 0 或未设时，按墙厚分布和图幅推断，并在 `units.json` / `summary.txt` 写明依据。换算只发生在输出目录的工作副本上，源 DXF/DWG 不动。`--viewport` 用源文件坐标，脚本同步缩放。后续出图（Q 清单、一屏审核）必须用工作副本和换算后的视口（见 `units.json` 的 `working_source`、`viewport_mm`）。
- **读写与检查：** `ezdxf` 导入、写 DXF、`doc.audit()`；保存后再打开审核一次。
- **LWPOLYLINE：** 读写点必须用 `format="xyb"`，否则凸度丢失，弧段会变成折线。
- **导入句柄会变：** 交叉检查、清单、规则例外一律用源句柄。
- **INSERT 块：** 按块归属整颗归类；同名块在 Q 里合并问一次。不要漏掉块外附属线（床头板、窗帘轨等）。
- **WIPEOUT：** 块里的遮罩可能把图「挖空」，需要时删掉再渲染。
- **线型：** 虚线门扇、窗帘盒在小比例图上可能看起来像实线；DXF 保留原线型，清单注明。
- **不要**把示意图、其它单元、其它楼层的几何拷进本户。

## 单类逐步模式（备选）

仅当用户希望「先把墙看完再看门」或需要逐类精修时使用。顺序仍建议：墙体（含窗、门框墙）→ 柜体 → 洁具 → 电器 → 门 → 家具 → 灯具 → 合并。每一类出审核图 + 独立 DXF + 编号清单，等用户定稿（或明确「先往下」）再开下一类。已定稿句柄不得被后类改掉；必须改则出该类别新版并更新合并图。批量模式下已生成的未归类清单和规则文件仍要沿用，不要丢掉。

## 附带脚本

草稿在 `scripts/` 与 `rules.yaml`，不绑定某一户路径。需要 `ezdxf`、`cairosvg`、Pillow、PyYAML。

| 文件 | 作用 |
| --- | --- |
| `rules.yaml` | 默认规则种子 + `learned` 沉淀处 |
| `scripts/classify.py` | 读 `$INSUNITS` 并按需换算为毫米，按规则初分类；输出分类结果、未归类、Q 清单、`units.json`；可与已定稿对照算命中率 |
| `scripts/compose_review.py` | 一屏审核图：按类着色总览 + 编号 + 各类缩略 |
| `scripts/render_review.py` | 灰底 + 高亮本类 + 黄标签（可选金 Q）出单类审核 PNG |
| `scripts/dxftools.py` | `audit` / `layers` / `xyb` 回写；按句柄导入分层；源句柄重叠检查 |
| `scripts/merge_master.py` | 按清单合并正式图层，未归类进「其他」，检查撞色与 audit |
| `scripts/dwg2dxf.mjs` | 本地 DWG→DXF，不依赖 cwd，不改源文件 |

```bash
S=/home/box/agent-data/workflows/cad-floorplan-decompose
cp $S/rules.yaml project/rules.yaml

# DWG→DXF（任意目录均可运行；不要改源 DWG）
node $S/scripts/dwg2dxf.mjs source.dwg project/source.dxf

# 初分类（视口按源文件坐标；非毫米时会在 out-dir 写 source-mm.dxf）
python $S/scripts/classify.py --source project/source.dxf --rules project/rules.yaml \
    --viewport xmin,ymin,xmax,ymax --out-dir out/class1 --q-crops --font NotoSansCJKSC.otf

# 一屏审核（用 units.json 里的工作副本和换算后视口）
python $S/scripts/compose_review.py --units out/class1/units.json \
    --classification out/class1/classification.json --questions out/class1/questions.json \
    --font NotoSansCJKSC.otf --out out/class1/review.png \
    --title '住宅平面初分类 第1轮审核'

# 用户答完 Q、写入 project/rules.yaml 的 learned 后，再跑一遍
python $S/scripts/classify.py --source project/source.dxf --rules project/rules.yaml \
    --viewport xmin,ymin,xmax,ymax --out-dir out/class2 --q-crops --font NotoSansCJKSC.otf

# 按句柄出某一类 DXF / 单类审核图 / 合并
python $S/scripts/dxftools.py import-handles --source project/source.dxf --map furniture-layers.json \
    --out furniture_v1.dxf --underlay 原图底图
python $S/scripts/render_review.py --source out/class1/source-mm.dxf --viewport xmin,ymin,xmax,ymax \
    --font NotoSansCJKSC.otf --highlight-handles furniture-handles.json \
    --highlight-color '#B266FF' --title '家具 第1版' --out furniture_v1.png \
    --labels furniture-labels.json
python $S/scripts/dxftools.py overlap walls.json cabinets.json doors.json furniture.json
python $S/scripts/merge_master.py --manifest merge.json --source project/source.dxf --out-dxf merge_v1.dxf
```

注意：若做了单位换算，`render_review.py` / 出图也要用 `units.json` 里的 `working_source` 和 `viewport_mm`，不要再用未缩放的源坐标去套已经缩放的 DXF。

`classify.py` 在 `--out-dir` 写出 `classification.json`、`unclassified.json`、`questions.json`、`summary.txt`、`units.json`；源图不是毫米时另写 `source-mm.dxf`。若提供 `--gold '{类别: [源句柄…]}'`，另写 `report.txt` 做命中率对照（仅用于自测或回归，不要把某户句柄写进规则种子）。`--q-crops` 另写 `q-sheet.png`（项多时 `q-sheet-02.png`）。出图后仍然要 Read 核对 PNG，确认金色高亮、尺寸和候选可读。
