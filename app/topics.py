"""选题：微博热搜抓取 + 粗筛 + 去重。

选题闸门在「写」这一步真正生效的是 checks + 人工确认；
这里只负责把候选拉回来、做硬性排除、和选题库去重。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from . import db

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

HOT_SEARCH = "https://weibo.com/ajax/side/hotSearch"
HOT_BAND = "https://weibo.com/ajax/statuses/hot_band"
TOPHUB = "https://tophub.today/n/KqndgxeLl9"

# 硬性排除：涉政、民族宗教、未定性案件、军事冲突
HARD_BLOCK = [
    "习近平", "总书记", "中央", "人大", "政协", "国务院", "外交部", "国防部",
    "台独", "港独", "疆独", "藏独", "两岸", "一中", "统战",
    "新疆", "西藏", "宗教", "穆斯林", "清真", "活佛", "民族政策",
    "军队", "军区", "解放军", "武警", "导弹", "军演", "战争", "停火",
    "俄乌", "普京", "泽连斯基", "以色列", "加沙", "哈马斯", "伊朗",
    "落马", "双开", "纪委", "监委", "受审", "判刑", "死刑",
]
# 明显不适合做本号的品类
SOFT_BLOCK = [
    "官宣", "恋情", "复合", "分手", "结婚", "离婚", "孕", "生子",
    "综艺", "选秀", "演唱会", "塌房", "解约", "番位", "粉丝",
    "赛程", "夺冠", "晋级", "比分", "联赛", "转会",
]

MIN_HEAT = 150_000

# --------------------------------------------------------------------------
# 分类
# --------------------------------------------------------------------------
# 微博主榜接口不给分类字段（只有 word/num/label_name），所以按关键词规则归类。
# 顺序即优先级：一条同时命中"游戏"和"电竞"都归 ACG；"手机发布会"归科技不归文娱。

CATEGORIES = ("文娱", "社会", "科技", "财经", "教育", "健康", "生活", "体育", "ACG", "其他")

CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ACG", (
        "动漫", "番剧", "二次元", "漫展", "手办", "cosplay", "cos", "游戏", "电竞",
        "手游", "端游", "主机", "原神", "崩坏", "王者荣耀", "英雄联盟", "lol", "steam",
        "任天堂", "switch", "ps5", "xbox", "宝可梦", "联动", "同人", "漫画", "动画",
        "剧场版", "声优", "乙女", "抽卡", "开服", "版本更新", "虚拟主播", "vtuber",
        "米哈游", "腾讯游戏", "网易游戏", "黑神话", "独立游戏",
    )),
    ("体育", (
        "夺冠", "联赛", "球队", "球员", "进球", "比分", "赛季", "世界杯", "奥运",
        "亚运", "全运", "nba", "cba", "中超", "国足", "男足", "女足", "女排", "男排",
        "乒乓", "羽毛球", "网球", "马拉松", "退役", "转会", "主教练", "赛场", "决赛",
        "半决赛", "小组赛", "犯规", "红牌", "体操", "跳水", "举重", "田径", "击剑",
        "滑雪", "世锦赛", "大满贯", "体育", "球员", "足协", "篮协",
    )),
    ("财经", (
        "股市", "a股", "港股", "美股", "大盘", "沪指", "上证", "深成指", "创业板",
        "黄金", "金价", "汇率", "人民币", "美元", "美联储", "降息", "加息", "债市",
        "基金", "理财", "存款", "利率", "通胀", "cpi", "gdp", "财报", "业绩",
        "比特币", "加密货币", "楼市", "央行", "货币政策", "ipo", "市值", "涨停",
        "跌停", "大宗商品", "期货", "房价跌", "降准", "熔断", "证券", "券商",
    )),
    ("科技", (
        "人工智能", "大模型", "芯片", "算力", "手机", "华为", "苹果", "小米", "荣耀",
        "oppo", "vivo", "特斯拉", "自动驾驶", "机器人", "发布会", "数码", "显卡",
        "处理器", "操作系统", "安卓", "鸿蒙", "ios", "卫星", "火箭", "航天", "量子",
        "电池", "新能源", "充电", "续航", "开源", "程序员", "代码", "算法", "5g", "6g",
        "星链", "meta", "openai", "chatgpt", "deepseek", "英伟达", "台积电", "ai",
        "智能体", "agent", "数据中心", "半导体", "光刻", "黑科技", "折叠屏", "眼镜",
    )),
    ("教育", (
        "高考", "中考", "考研", "考公", "四六级", "留学", "招生", "开学", "寒假",
        "暑假", "学费", "补课", "双减", "教师", "老师", "学校", "大学", "录取",
        "志愿", "专业", "校招", "论文", "学历", "幼儿园", "托管", "作业", "课本",
        "教材", "院士", "博士", "硕士", "奖学金", "宿舍", "校园", "家长会",
    )),
    ("健康", (
        "医保", "疫情", "流感", "疫苗", "医院", "医生", "护士", "癌症", "疾病",
        "药品", "体检", "心理健康", "抑郁", "焦虑", "猝死", "手术", "门诊",
        "挂号", "养生", "血压", "血糖", "减肥", "睡眠", "营养", "过敏", "发烧",
        "感染", "病毒", "器官", "献血", "急救", "医美",
    )),
    ("文娱", (
        "明星", "演员", "歌手", "综艺", "电视剧", "电影", "票房", "导演", "演唱会",
        "新歌", "专辑", "单曲", "塌房", "官宣", "恋情", "结婚", "离婚", "分手",
        "粉丝", "红毯", "颁奖", "晚会", "片酬", "剧组", "剧本", "网剧", "首映",
        "角色", "杀青", "主持", "顶流", "偶像", "选秀", "跨年", "春晚", "歌单",
        "巡演", "代言", "路透", "开机", "预告", "番位", "造型", "look", "恋情",
        "绯闻", "艺人", "经纪人", "工作室", "剧集", "短剧", "音乐节", "livehouse",
    )),
    ("生活", (
        "天气", "降温", "暴雨", "台风", "高温", "寒潮", "菜价", "油价", "房价",
        "租房", "房租", "医保", "社保", "养老金", "开学", "高考", "考研", "放假",
        "调休", "招聘", "工资", "加班", "快递", "外卖", "旅游", "景区", "机票",
        "高铁", "地铁", "航班", "食谱", "美食", "购物", "消费券", "以旧换新",
        "超市", "菜市场", "水电费", "物业费", "个税", "公积金", "年假", "社保卡",
    )),
    ("社会", (
        "通报", "警方", "公安", "法院", "判决", "起诉", "失联", "坠亡", "事故",
        "车祸", "火灾", "塌方", "爆炸", "维权", "赔偿", "诈骗", "盗窃", "打人",
        "纠纷", "举报", "调查", "处罚", "罚款", "拘留", "救人", "见义勇为",
        "业主", "物业", "学校", "老师", "学生", "霸凌", "食堂", "村民", "镇政府",
        "刑拘", "立案", "曝光", "乱象", "违规", "造假", "救援", "遇难", "走失",
        "讨薪", "欠薪", "中介", "直播间", "带货", "反诈",
    )),
]

_ASCII_KW = re.compile(r"^[a-z0-9.]+$")


def _hit(word: str, kw: str) -> bool:
    """中文按子串匹配；英文/数字要求单词边界，避免 ai 命中 waiting。"""
    if _ASCII_KW.match(kw):
        return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", word) is not None
    return kw in word


def classify_category(word: str) -> str:
    text = (word or "").lower()
    for cat, kws in CATEGORY_RULES:
        if any(_hit(text, k) for k in kws):
            return cat
    return "其他"


def category_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {c: 0 for c in CATEGORIES}
    for it in items:
        counts[it.get("category") or "其他"] = counts.get(it.get("category") or "其他", 0) + 1
    return counts


def category_for_domain(domain: str) -> str:
    """账号的「选题领域」映射到默认分类标签，用于热搜页自动切 tab。"""
    d = (domain or "").lower()
    if not d:
        return ""
    table = (
        ("ACG", ("游戏", "动漫", "电竞", "二次元", "acg")),
        ("体育", ("体育", "足球", "篮球", "赛事", "运动")),
        ("财经", ("财经", "金融", "股票", "投资", "理财", "经济", "商业", "楼市")),
        ("教育", ("教育", "升学", "考试", "校园", "教师", "育儿")),
        ("健康", ("健康", "医疗", "养生", "医学", "疾病")),
        ("财经", (
        "股市", "a股", "港股", "美股", "大盘", "沪指", "上证", "深成指", "创业板",
        "黄金", "金价", "汇率", "人民币", "美元", "美联储", "降息", "加息", "债市",
        "基金", "理财", "存款", "利率", "通胀", "cpi", "gdp", "财报", "业绩",
        "比特币", "加密货币", "楼市", "央行", "货币政策", "ipo", "市值", "涨停",
        "跌停", "大宗商品", "期货", "房价跌", "降准", "熔断", "证券", "券商",
    )),
    ("科技", ("科技", "ai", "芯片", "数码", "互联网", "手机", "大模型", "机器", "自动")),
        ("教育", (
        "高考", "中考", "考研", "考公", "四六级", "留学", "招生", "开学", "寒假",
        "暑假", "学费", "补课", "双减", "教师", "老师", "学校", "大学", "录取",
        "志愿", "专业", "校招", "论文", "学历", "幼儿园", "托管", "作业", "课本",
        "教材", "院士", "博士", "硕士", "奖学金", "宿舍", "校园", "家长会",
    )),
    ("健康", (
        "医保", "疫情", "流感", "疫苗", "医院", "医生", "护士", "癌症", "疾病",
        "药品", "体检", "心理健康", "抑郁", "焦虑", "猝死", "手术", "门诊",
        "挂号", "养生", "血压", "血糖", "减肥", "睡眠", "营养", "过敏", "发烧",
        "感染", "病毒", "器官", "献血", "急救", "医美",
    )),
    ("文娱", ("文娱", "娱乐", "影视", "明星", "音乐", "综艺")),
        ("生活", ("生活", "健康", "美食", "旅行", "教育", "职场", "消费", "时尚", "穿搭", "美妆")),
        ("社会", ("社会", "民生", "新闻", "评论")),
    )
    for cat, kws in table:
        if any(k in d for k in kws):
            return cat
    return ""


# --------------------------------------------------------------------------
# 让模型分类（关键词覆盖不到明星名和口语化短句，必须靠语义）
# --------------------------------------------------------------------------

_CAT_CACHE: dict[str, str] = {}

CATEGORY_PROMPT = """把下面这些微博热搜词条归到 9 个分类之一。

分类定义：
- 文娱：影视剧、综艺、音乐、明星艺人、演唱会、票房、短剧、颁奖等娱乐内容
- 社会：公共事件、案件通报、事故、维权赔偿、教育医疗纠纷、行业乱象、民生政策争议
- 科技：AI/大模型、芯片、手机数码、汽车与自动驾驶、互联网平台、航天、机器人等科技话题
- 财经：股市行情、金价汇率、美联储与降息、基金理财、楼市房价、公司财报市值、加密资产等
- 教育：高考中考考研考公、招生录取、学校教师学生、学费补课、留学论文、校园事件
- 健康：疾病疫情疫苗、医院医生挂号、医保药品、心理健康、减肥睡眠养生、医美急救
- 生活：天气、物价、交通出行、房价房租、社保医保、健康养生、美食购物、放假调休、育儿上学
- 体育：各类竞技赛事、球队球员、夺冠比分、奥运联赛、运动员个人
- ACG：动画漫画、游戏电竞、二次元、手办漫展、虚拟主播、游戏版本与厂商

判断优先级：一个人名如果主要是演员/歌手/偶像，归文娱；如果是运动员，归体育；
如果词条本身在讲游戏或动漫作品，归 ACG（哪怕涉及明星配音）。

词条：
{words}

只输出 JSON 对象，键是词条原文（一字不改），值是上面 6 个分类名之一。不要输出别的：
{{"词条1": "文娱", "词条2": "社会"}}
"""


def classify_words(words: list[str], use_llm: bool = True) -> dict[str, str]:
    """只做分类：命缓存，未命中的批量问模型。返回 {词条: 分类}。"""
    words = [w for w in dict.fromkeys(words) if w]
    out = {w: _CAT_CACHE[w] for w in words if w in _CAT_CACHE}
    todo = [w for w in words if w not in _CAT_CACHE]
    if not use_llm or not todo:
        return out

    from . import writer  # 局部导入，避免模块循环

    import json as _json

    try:
        raw = writer.chat([
            {"role": "system", "content": "你是中文新闻编辑，擅长给热词归类，只输出 JSON。"},
            {"role": "user", "content": CATEGORY_PROMPT.format(
                words="\n".join(f"- {w}" for w in todo[:45]))},
        ], json_mode=True, temperature=0.2, timeout=120)
        data = _json.loads(raw)
    except Exception:  # noqa: BLE001 - 分类失败不影响其它功能
        return out

    if isinstance(data, dict):
        valid = set(CATEGORIES)
        for w, cat in data.items():
            cat = str(cat).strip()
            if cat in valid:
                _CAT_CACHE[str(w)] = cat
                out[str(w)] = cat
    return out


def refine_categories(items: list[dict[str, Any]], use_llm: bool = True) -> list[dict[str, Any]]:
    """先用规则分类，再让模型覆盖不确定的那些（结果进内存缓存，同一词条只问一次）。"""
    pending: list[str] = []
    for it in items:
        word = it["word"]
        cached = _CAT_CACHE.get(word)
        if cached:
            it["category"] = cached
        elif it.get("category") in (None, "", "其他") and word not in pending:
            pending.append(word)

    if not use_llm or not pending:
        return items

    from . import writer  # 局部导入，避免模块循环

    try:
        import json as _json

        out = writer.chat([
            {"role": "system", "content": "你是中文新闻编辑，擅长给热词归类，只输出 JSON。"},
            {"role": "user", "content": CATEGORY_PROMPT.format(
                words="\n".join(f"- {w}" for w in pending[:45]))},
        ], json_mode=True, temperature=0.2, timeout=120)
        data = _json.loads(out)
    except Exception:  # noqa: BLE001 - 分类失败不影响抓热搜
        return items

    if not isinstance(data, dict):
        return items

    valid = set(CATEGORIES)
    by_word = {it["word"]: it for it in items}
    for word, cat in data.items():
        cat = str(cat).strip()
        if cat not in valid:
            continue
        _CAT_CACHE[word] = cat
        target = by_word.get(word) or by_word.get(str(word).strip())
        if target is not None:
            target["category"] = cat
    return items


def fetch_weibo(limit: int = 50) -> list[dict[str, Any]]:
    """主接口 → hot_band → tophub，逐级降级。"""
    items: list[dict[str, Any]] = []
    try:
        r = requests.get(
            HOT_SEARCH,
            headers={"User-Agent": UA, "Referer": "https://weibo.com/"},
            timeout=20,
        )
        d = r.json()
        realtime = (d.get("data") or {}).get("realtime") or []
        for idx, it in enumerate(realtime[:limit]):
            items.append({
                "rank": it.get("realpos") or (idx + 1),
                "word": (it.get("word") or "").strip(),
                "num": int(it.get("num") or 0),
                "label": it.get("label_name") or "",
                "note": it.get("note") or "",
                "source": "weibo",
            })
    except (requests.RequestException, ValueError):
        items = []

    if not items:
        try:
            r = requests.get(
                HOT_BAND, headers={"User-Agent": UA, "Referer": "https://weibo.com/"},
                timeout=20,
            )
            d = r.json()
            band = ((d.get("data") or {}).get("band_list")) or []
            for idx, it in enumerate(band[:limit]):
                items.append({
                    "rank": idx + 1,
                    "word": (it.get("word") or "").strip(),
                    "num": int(it.get("num") or 0),
                    "label": it.get("label_name") or "",
                    "note": "",
                    "source": "weibo_band",
                })
        except (requests.RequestException, ValueError):
            items = []

    if not items:
        items = _fetch_tophub(limit)

    return [i for i in items if i["word"]]


def _fetch_tophub(limit: int) -> list[dict[str, Any]]:
    try:
        r = requests.get(TOPHUB, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
    except requests.RequestException:
        return []
    out: list[dict[str, Any]] = []
    for m in re.finditer(r'href="(https://s\.weibo\.com[^"]*)"[^>]*>(.*?)</a>', r.text)[:limit]:
        word = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if word:
            out.append({"rank": len(out) + 1, "word": word, "num": 0,
                        "label": "", "note": "", "source": "tophub"})
    return out


def classify(item: dict[str, Any]) -> dict[str, Any]:
    word = item["word"]
    hard = [k for k in HARD_BLOCK if k in word]
    soft = [k for k in SOFT_BLOCK if k in word]
    heat_ok = item["num"] >= MIN_HEAT or item["num"] == 0
    return {
        **item,
        "category": classify_category(word),
        "hard_block": hard,
        "soft_block": soft,
        "heat_ok": heat_ok,
        "usable": not hard and heat_ok,
        "caution": bool(soft),
    }


def candidates(limit: int = 25) -> list[dict[str, Any]]:
    raw = fetch_weibo(limit=max(limit, 30))
    out = [classify(i) for i in raw]
    return out[:limit]


def candidates_text(items: list[dict[str, Any]], max_items: int = 25) -> str:
    lines = []
    for i, it in enumerate([x for x in items if x["usable"]][:max_items], 1):
        heat = f"{it['num']:,}" if it["num"] else "—"
        flag = " ·争议" if it["caution"] else ""
        lines.append(f"{i}. {it['word']} | {heat} | {it['label']}{flag}")
    return "\n".join(lines)


def already_written_titles(days: int = 14) -> list[str]:
    rows = db.query(
        "SELECT title, day FROM topics ORDER BY id DESC LIMIT ?", (days * 3,)
    )
    return [f"{r['day']} {r['title']}" for r in rows]


def remember(day: str, slot: str, title: str, *, source: str = "weibo",
             rank: int | None = None, heat: int | None = None,
             keywords: list[str] | None = None, note: str = "",
             article_id: int | None = None) -> int:
    existing = db.query_one(
        "SELECT id FROM topics WHERE day=? AND slot=? AND title=?", (day, slot, title)
    )
    if existing:
        return int(existing["id"])
    return db.insert("topics", {
        "day": day, "slot": slot, "title": title, "source": source,
        "weibo_rank": rank, "weibo_heat": heat,
        "keywords": json.dumps(keywords or [], ensure_ascii=False),
        "note": note, "article_id": article_id, "created_at": db.now(),
    })


def slot_has_article(day: str, slot: str) -> bool:
    row = db.query_one("SELECT id FROM topics WHERE day=? AND slot=?", (day, slot))
    return bool(row)


def recent(limit: int = 30) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM topics ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["keywords"] = json.loads(d.get("keywords") or "[]")
        except ValueError:
            d["keywords"] = []
        out.append(d)
    return out
