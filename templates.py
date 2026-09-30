"""静态文案与展示常量 / Static templates & display constants.

这个模块**零依赖**（不 import 项目内其他模块），因此可以被引擎层、视图层、
键盘层、甚至 Web 看板安全地引用而不产生循环导入。

只放「真正静态」的东西：分隔符、品牌名、联赛名表、状态文案、菜单项、
免责声明、价值偏差阈值。**带条件的动态组装不在这里**——那属于 views/，
因为文案与生成它的逻辑放在一起才是可维护的：改一句文案只动一个文件，
不需要在模板文件和组装代码之间来回跳。
"""
from __future__ import annotations



SEP = "━━━━━━━━━━━━━━━━━━"

THIN_SEP = "──────────────────"  # 次级分隔：用于分区内部，避免主分隔线过度重复

BULLET = "▸"  # 列表符号

BRAND_EN = "FOOTBALL INSIGHT"

BRAND_CN = "Football Insight"

LEAGUE_NAMES = {
    39: "英格兰超级联赛",
    140: "西班牙甲级联赛",
    78: "德国甲级联赛",
    135: "意大利甲级联赛",
    61: "法国甲级联赛",
    2: "欧洲冠军联赛",
    88: "荷兰甲级联赛",
    94: "葡萄牙超级联赛",
    40: "英格兰冠军联赛",
    71: "巴西甲级联赛",
    1: "国际足联世界杯",
    4: "欧洲足球锦标赛",
}

TEAM_NAMES: dict[str, str] = {
    # 英超 / Premier League
    "Manchester City FC": "曼城", "Liverpool FC": "利物浦", "Arsenal FC": "阿森纳",
    "Manchester United FC": "曼联", "Chelsea FC": "切尔西", "Tottenham Hotspur FC": "托特纳姆热刺",
    "Newcastle United FC": "纽卡斯尔联", "Brighton & Hove Albion FC": "布莱顿",
    "Aston Villa FC": "阿斯顿维拉", "West Ham United FC": "西汉姆联",
    "Crystal Palace FC": "水晶宫", "Everton FC": "埃弗顿", "Fulham FC": "富勒姆",
    "Brentford FC": "布伦特福德", "Nottingham Forest FC": "诺丁汉森林",
    "AFC Bournemouth": "伯恩茅斯", "Wolverhampton Wanderers FC": "狼队",
    "Leeds United FC": "利兹联", "Sunderland AFC": "桑德兰", "Burnley FC": "伯恩利",
    # 西甲 / La Liga
    "Real Madrid CF": "皇家马德里", "FC Barcelona": "巴塞罗那",
    "Club Atlético de Madrid": "马德里竞技", "Sevilla FC": "塞维利亚",
    "Real Betis Balompié": "皇家贝蒂斯", "Valencia CF": "瓦伦西亚",
    "Villarreal CF": "比利亚雷亚尔", "Athletic Club": "毕尔巴鄂竞技",
    "Real Sociedad de Fútbol": "皇家社会", "RC Celta de Vigo": "塞尔塔",
    # 德甲 / Bundesliga
    "FC Bayern München": "拜仁慕尼黑", "Borussia Dortmund": "多特蒙德",
    "RB Leipzig": "莱比锡红牛", "Bayer 04 Leverkusen": "勒沃库森",
    "Eintracht Frankfurt": "法兰克福", "VfB Stuttgart": "斯图加特",
    "Borussia Mönchengladbach": "门兴格拉德巴赫", "VfL Wolfsburg": "沃尔夫斯堡",
    # 意甲 / Serie A
    "Juventus FC": "尤文图斯", "FC Internazionale Milano": "国际米兰",
    "AC Milan": "AC米兰", "SSC Napoli": "那不勒斯", "AS Roma": "罗马",
    "SS Lazio": "拉齐奥", "Atalanta BC": "亚特兰大", "ACF Fiorentina": "佛罗伦萨",
    # 法甲 / Ligue 1
    "Paris Saint-Germain FC": "巴黎圣日耳曼", "Olympique de Marseille": "马赛",
    "Olympique Lyonnais": "里昂", "AS Monaco FC": "摩纳哥", "LOSC Lille": "里尔",
}

OUTCOME_LABEL = {"home": "主胜", "draw": "平局", "away": "客胜"}

DISCLAIMER = "⚠️ 模型仅基于进球数据估算，不构成投注建议。"

VALUE_FLAG = 0.05  # 价值偏差超过此值时打 🚀 标记

VALUE_HIGH = 0.07

VALUE_LOW = 0.03

TABS = (("home", "📈 预测"), ("deep", "🔍 深度分析"), ("h2h", "📊 历史交锋"), ("odds", "💰 赔率对比"))

MENU_ITEMS = (
    ("fixtures", "📅 今日赛程"),
    ("predict", "⚽ 比赛预测"),
    ("analysis", "📊 深度分析"),
    ("standings", "🏆 联赛排名"),
    ("refresh", "🔄 刷新数据"),
    ("help", "ℹ️ 使用帮助"),
    ("web", "🌐 网页端"),
    ("storage", "💾 存储状态"),
)

# ---- 底部面板的收放控制 ---------------------------------------------------------
# 面板占屏幕近一半高度，与内联键盘同时出现会挤掉正文。因此把它做成可收放的：
# 收起后键盘整个移出屏幕，需要时通过客户端的键盘图标或 /panel 唤回。
# 这两个标签必须与 MENU_ITEMS 的文字不重合，否则 _menu_key_from_text 会把它
# 误判成菜单项。
PANEL_OPEN_LABEL = "☰ 菜单"
PANEL_CLOSE_LABEL = "✖️ 收起面板"

PANEL_EXPANDED = "expanded"
PANEL_COLLAPSED = "collapsed"

PANEL_HINTS = {
    PANEL_EXPANDED: "⌨️ <b>功能面板已展开</b>\n直接点下方按钮开始；不需要时点「✖️ 收起面板」，把屏幕还给正文。",
    PANEL_COLLAPSED: (
        "⌨️ <b>面板已收起</b>\n"
        "键盘已移出屏幕。命令（如 /fixtures、/predict）照常可用；\n"
        "需要按钮时：点输入框旁的 ⌨️ 图标，或发送 /panel。"
    ),
}

STATUS_TEXT = {
    "NS": "未开始",
    "TBD": "时间待定",
    "1H": "上半场",
    "HT": "中场休息",
    "2H": "下半场",
    "ET": "加时赛",
    "BT": "加时休息",
    "P": "点球大战",
    "PEN": "点球大战",
    "FT": "已完场",
    "AET": "加时完场",
    "PST": "已推迟",
    "CANC": "已取消",
    "ABD": "已中止",
    "SUSP": "已中断",
    "INT": "已中断",
    "LIVE": "进行中",
    "WO": "弃赛",
}

NO_DATA = "暂无可靠数据，不参与本次分析（缺什么就说明缺什么，不做填充）。"
