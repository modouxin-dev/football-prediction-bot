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

# 联赛编号 → 中文名（行尾注释为官方英文名，便于对照 API 与文档）。
# 编号取自 API-Football（api_football_id），与数据源严格对应，不要凭印象改
# ——写错编号只会静默拉到空数据，不会报错。新增联赛请同时补中文名、英文名
# 注释，以及下方 LEAGUE_SHORT / LEAGUE_ORDER（缺一会导致显示成裸编号）。
#
# 为什么从 12 个扩到 50 个：五大联赛在休赛期 / 国际比赛日会整体停摆，
# 只挂五个联赛的话，那些天用户点开就是「暂无比赛」，会以为机器人坏了。
# 主流赛程站从不缺比赛，正因为它同时挂着几十个联赛——五大联赛休息时，
# 北欧、南美、亚洲的联赛照常开踢。扩池是「每天都有球看」的根本解法。
LEAGUE_NAMES = {
    # ── 五大联赛（默认启用）────────────────────────────
    39: "英格兰超级联赛",  # Premier League
    140: "西班牙甲级联赛",  # La Liga / Primera División
    78: "德国甲级联赛",  # Bundesliga
    135: "意大利甲级联赛",  # Serie A
    61: "法国甲级联赛",  # Ligue 1
    # ── 欧战 ────────────────────────────────────────
    2: "欧洲冠军联赛",  # UEFA Champions League
    3: "欧罗巴联赛",  # UEFA Europa League
    848: "欧洲协会联赛",  # UEFA Europa Conference League
    # ── 英格兰 / 次级 ────────────────────────────────
    40: "英格兰冠军联赛",  # EFL Championship
    41: "英格兰甲级联赛",  # EFL League One
    42: "英格兰乙级联赛",  # EFL League Two
    # ── 西欧 ────────────────────────────────────────
    141: "西班牙乙级联赛",   # Segunda División
    136: "意大利乙级联赛",   # Serie B
    62: "法国乙级联赛",     # Ligue 2
    79: "德国乙级联赛",     # 2. Bundesliga
    88: "荷兰甲级联赛",     # Eredivisie
    94: "葡萄牙超级联赛",   # Primeira Liga
    144: "比利时甲级联赛",   # Jupiler Pro League
    207: "瑞士超级联赛",  # Swiss Super League
    218: "奥地利甲级联赛",  # Austrian Bundesliga
    179: "苏格兰超级联赛",  # Scottish Premiership
    180: "苏格兰冠军联赛",  # Scottish Championship
    # ── 北欧 / 东欧 ──────────────────────────────────
    103: "挪威超级联赛",    # Eliteserien
    113: "瑞典超级联赛",    # Allsvenskan
    119: "丹麦超级联赛",  # Danish Superliga
    244: "芬兰超级联赛",  # Veikkausliiga
    106: "波兰甲级联赛",    # Ekstraklasa
    235: "俄罗斯超级联赛",  # Russian Premier League
    345: "捷克甲级联赛",  # Czech Fortuna Liga
    271: "匈牙利甲级联赛",  # Nemzeti Bajnokság I
    283: "罗马尼亚甲级联赛",  # Liga I
    210: "克罗地亚甲级联赛",  # HNL
    197: "希腊超级联赛",  # Super League Greece
    318: "塞浦路斯甲级联赛",  # Cypriot First Division
    # ── 南欧 / 地中海 ────────────────────────────────
    203: "土耳其超级联赛",  # Süper Lig
    357: "爱尔兰超级联赛",  # League of Ireland Premier Division
    383: "以色列超级联赛",  # Israeli Premier League
    # ── 美洲 ────────────────────────────────────────
    71: "巴西甲级联赛",  # Brasileirão Série A
    128: "阿根廷甲级联赛",  # Liga Profesional de Fútbol
    253: "美国职业大联盟",  # Major League Soccer
    262: "墨西哥甲级联赛",  # Liga MX
    239: "哥伦比亚甲级联赛",  # Categoría Primera A
    242: "厄瓜多尔甲级联赛",  # LigaPro Serie A
    250: "巴拉圭甲级联赛",  # Primera División
    # ── 亚洲 / 非洲 ──────────────────────────────────
    98: "日本职业联赛",  # J1 League
    292: "韩国甲级联赛",  # K League 1
    169: "中国超级联赛",  # Chinese Super League
    307: "沙特职业联赛",  # Saudi Pro League
    301: "阿联酋甲级联赛",  # UAE Pro League
    305: "卡塔尔星级联赛",  # Qatar Stars League
    233: "埃及超级联赛",  # Egyptian Premier League
    200: "摩洛哥甲级联赛",  # Botola Pro
    186: "阿尔及利亚甲级联赛",  # Algerian Ligue 1
    # ── 国家队赛事 ───────────────────────────────────
    1: "国际足联世界杯",  # FIFA World Cup
    4: "欧洲足球锦标赛",  # UEFA European Championship
}

# 按钮用联赛短名：中文全称在按钮上放不下（「英格兰超级联赛」= 14 列，
# 4 个按钮平分一行时每格仅约 8 列，会被截成「英格兰超…」——恰恰是
# 用来区分联赛的国别被挤没了）。未登记的联赛回退全称，绝不丢弃。
LEAGUE_SHORT: dict[int, str] = {
    39: "英超", 140: "西甲", 78: "德甲", 135: "意甲", 61: "法甲",
    2: "欧冠", 3: "欧联", 848: "欧协联",
    40: "英冠", 41: "英甲", 42: "英乙",
    141: "西乙", 136: "意乙", 62: "法乙", 79: "德乙",
    88: "荷甲", 94: "葡超", 144: "比甲", 207: "瑞士超", 218: "奥甲",
    179: "苏超", 180: "苏冠",
    103: "挪超", 113: "瑞超", 119: "丹超", 244: "芬超",
    106: "波兰甲", 235: "俄超", 345: "捷甲", 271: "匈甲",
    283: "罗甲", 210: "克甲", 197: "希腊超", 318: "塞甲",
    203: "土超", 357: "爱超", 383: "以超",
    71: "巴甲", 128: "阿甲", 253: "美职", 262: "墨甲",
    239: "哥甲", 242: "厄甲", 250: "巴拉甲",
    98: "日职", 292: "韩K", 169: "中超", 307: "沙特职",
    301: "阿联甲", 305: "卡塔尔星",
    233: "埃及超", 200: "摩洛哥甲", 186: "阿尔及甲",
    1: "世界杯", 4: "欧洲杯",
}

# 汇总页的联赛展示顺序：五大联赛在前（与主流观赛习惯一致），
# 欧战与其余联赛随后。未登记的联赛排最后并按 ID 升序（见 views/digest.py
# 的 _league_sort_key）。顺序只影响观感，不影响任何查询或落库。
# 汇总页的联赛展示顺序：五大联赛在前（与主流观赛习惯一致），欧战、
# 其余欧洲联赛、美洲、亚洲依次排列。未登记的联赛排最后并按 ID 升序
# （见 views/digest.py 的 _league_sort_key）。顺序只影响观感，不影响查询。
LEAGUE_ORDER: tuple[int, ...] = (
    39, 140, 78, 135, 61,              # 五大联赛
    2, 3, 848,                          # 欧战
    40, 41, 42, 141, 136, 62, 79,       # 英/西/意/法/德 次级
    88, 94, 144, 207, 218, 179, 180,    # 西欧
    103, 113, 119, 244, 106, 235, 345, 271, 283, 210, 197, 318,  # 北欧/东欧
    203, 357, 383,                      # 南欧/地中海
    71, 128, 253, 262, 239, 242, 250,   # 美洲
    98, 292, 169, 307, 301, 305,        # 亚洲
    233, 200, 186,                      # 非洲
    1, 4,                               # 国家队
)

# 汇总页分组标题用的图标：一屏十几条时，图标比文字定位更快。
# 每条只挂一个图标，不堆砌；未登记的联赛回退 ⚽。
# 汇总页分组标题用的图标：一屏十几条时，图标比文字定位更快。
# 每条只挂一个图标，不堆砌；未登记的联赛回退 ⚽。
LEAGUE_FLAGS: dict[int, str] = {
    39: "🏴󠁧󠁢󠁥󠁮󠁧󠁿", 140: "🇪🇸", 78: "🇩🇪", 135: "🇮🇹", 61: "🇫🇷",
    2: "🏆", 3: "🏆", 848: "🏆",
    40: "🏴󠁧󠁢󠁥󠁮󠁧󠁿", 41: "🏴󠁧󠁢󠁥󠁮󠁧󠁿", 42: "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    141: "🇪🇸", 136: "🇮🇹", 62: "🇫🇷", 79: "🇩🇪",
    88: "🇳🇱", 94: "🇵🇹", 144: "🇧🇪", 207: "🇨🇭", 218: "🇦🇹",
    179: "🏴󠁧󠁢󠁳󠁣󠁴󠁿", 180: "🏴󠁧󠁢󠁳󠁣󠁴󠁿",
    103: "🇳🇴", 113: "🇸🇪", 119: "🇩🇰", 244: "🇫🇮",
    106: "🇵🇱", 235: "🇷🇺", 345: "🇨🇿", 271: "🇭🇺",
    283: "🇷🇴", 210: "🇭🇷", 197: "🇬🇷", 318: "🇨🇾",
    203: "🇹🇷", 357: "🇮🇪", 383: "🇮🇱",
    71: "🇧🇷", 128: "🇦🇷", 253: "🇺🇸", 262: "🇲🇽",
    239: "🇨🇴", 242: "🇪🇨", 250: "🇵🇾",
    98: "🇯🇵", 292: "🇰🇷", 169: "🇨🇳", 307: "🇸🇦",
    301: "🇦🇪", 305: "🇶🇦",
    233: "🇪🇬", 200: "🇲🇦", 186: "🇩🇿",
    1: "🌍", 4: "🇪🇺",
}

TEAM_NAMES: dict[str, str] = {
    # 英超 / Premier League（20 队）
    "Manchester City FC": "曼城", "Liverpool FC": "利物浦", "Arsenal FC": "阿森纳",
    "Manchester United FC": "曼联", "Chelsea FC": "切尔西", "Tottenham Hotspur FC": "托特纳姆热刺",
    "Newcastle United FC": "纽卡斯尔联", "Brighton & Hove Albion FC": "布莱顿",
    "Aston Villa FC": "阿斯顿维拉", "West Ham United FC": "西汉姆联",
    "Crystal Palace FC": "水晶宫", "Everton FC": "埃弗顿", "Fulham FC": "富勒姆",
    "Brentford FC": "布伦特福德", "Nottingham Forest FC": "诺丁汉森林",
    "AFC Bournemouth": "伯恩茅斯", "Wolverhampton Wanderers FC": "狼队",
    "Leeds United FC": "利兹联", "Sunderland AFC": "桑德兰", "Burnley FC": "伯恩利",
    # 西甲 / La Liga（22 队，含升降级常见队）
    "Real Madrid CF": "皇家马德里", "Real Madrid": "皇家马德里",
    "FC Barcelona": "巴塞罗那", "Barcelona": "巴塞罗那",
    "Club Atlético de Madrid": "马德里竞技", "Atletico Madrid": "马德里竞技",
    "Sevilla FC": "塞维利亚", "Real Betis Balompié": "皇家贝蒂斯",
    "Real Betis": "皇家贝蒂斯", "Valencia CF": "瓦伦西亚",
    "Villarreal CF": "比利亚雷亚尔", "Athletic Club": "毕尔巴鄂竞技",
    "Real Sociedad de Fútbol": "皇家社会", "Real Sociedad": "皇家社会",
    "RC Celta de Vigo": "塞尔塔", "Celta de Vigo": "塞尔塔",
    "Getafe CF": "赫塔菲", "CA Osasuna": "奥萨苏纳", "Osasuna": "奥萨苏纳",
    "Rayo Vallecano": "巴列卡诺", "RCD Mallorca": "马略卡", "Mallorca": "马略卡",
    "Girona FC": "赫罗纳", "Deportivo Alavés": "阿拉维斯", "Alaves": "阿拉维斯",
    "UD Las Palmas": "拉斯帕尔马斯", "RCD Espanyol": "西班牙人", "Espanyol": "西班牙人",
    "Real Valladolid": "巴拉多利德", "CD Leganés": "莱加内斯", "Leganes": "莱加内斯",
    "Elche CF": "埃尔切", "Levante UD": "莱万特", "Real Oviedo": "奥维耶多",
    # 德甲 / Bundesliga（20 队）
    "FC Bayern München": "拜仁慕尼黑", "Bayern Munich": "拜仁慕尼黑",
    "Borussia Dortmund": "多特蒙德", "RB Leipzig": "莱比锡红牛",
    "Bayer 04 Leverkusen": "勒沃库森", "Bayer Leverkusen": "勒沃库森",
    "Eintracht Frankfurt": "法兰克福", "VfB Stuttgart": "斯图加特",
    "Borussia Mönchengladbach": "门兴格拉德巴赫",
    "Borussia Monchengladbach": "门兴格拉德巴赫",
    "VfL Wolfsburg": "沃尔夫斯堡", "SC Freiburg": "弗赖堡",
    "SV Werder Bremen": "云达不莱梅", "Werder Bremen": "云达不莱梅",
    "1. FSV Mainz 05": "美因茨", "FSV Mainz 05": "美因茨", "Mainz 05": "美因茨",
    "TSG 1899 Hoffenheim": "霍芬海姆", "TSG Hoffenheim": "霍芬海姆",
    "1. FC Union Berlin": "柏林联合", "Union Berlin": "柏林联合",
    "1. FC Köln": "科隆", "FC Koln": "科隆", "1. FC Koln": "科隆",
    "FC Augsburg": "奥格斯堡", "VfL Bochum 1848": "波鸿", "VfL Bochum": "波鸿",
    "FC St. Pauli": "圣保利", "Holstein Kiel": "荷尔斯泰因基尔",
    "1. FC Heidenheim 1846": "海登海姆", "1. FC Heidenheim": "海登海姆",
    "SV Darmstadt 98": "达姆施塔特", "Hamburger SV": "汉堡",
    # 意甲 / Serie A（21 队）
    "Juventus FC": "尤文图斯", "Juventus": "尤文图斯",
    "FC Internazionale Milano": "国际米兰", "Inter Milan": "国际米兰", "Inter": "国际米兰",
    "AC Milan": "AC米兰", "Milan": "AC米兰", "SSC Napoli": "那不勒斯", "Napoli": "那不勒斯",
    "AS Roma": "罗马", "Roma": "罗马", "SS Lazio": "拉齐奥", "Lazio": "拉齐奥",
    "Atalanta BC": "亚特兰大", "Atalanta": "亚特兰大",
    "ACF Fiorentina": "佛罗伦萨", "Fiorentina": "佛罗伦萨",
    "Bologna FC 1909": "博洛尼亚", "Bologna": "博洛尼亚",
    "Torino FC": "都灵", "Torino": "都灵", "Udinese Calcio": "乌迪内斯",
    "US Lecce": "莱切", "Lecce": "莱切", "Genoa CFC": "热那亚", "Genoa": "热那亚",
    "Cagliari Calcio": "卡利亚里", "Cagliari": "卡利亚里",
    "Hellas Verona FC": "维罗纳", "Verona": "维罗纳",
    "Parma Calcio 1913": "帕尔马", "Parma": "帕尔马",
    "Como 1907": "科莫", "Como": "科莫", "Venezia FC": "威尼斯", "Venezia": "威尼斯",
    "US Sassuolo Calcio": "萨索洛", "Sassuolo": "萨索洛",
    "Pisa SC": "比萨", "Pisa": "比萨", "US Cremonese": "克雷莫纳", "Cremonese": "克雷莫纳",
    # 法甲 / Ligue 1（19 队）
    "Paris Saint-Germain FC": "巴黎圣日耳曼", "Paris Saint Germain": "巴黎圣日耳曼",
    "Paris SG": "巴黎圣日耳曼", "PSG": "巴黎圣日耳曼",
    "Olympique de Marseille": "马赛", "Marseille": "马赛",
    "Olympique Lyonnais": "里昂", "Lyon": "里昂", "Lyon OLY": "里昂",
    "AS Monaco FC": "摩纳哥", "Monaco": "摩纳哥",
    "LOSC Lille": "里尔", "Lille": "里尔", "OGC Nice": "尼斯", "Nice": "尼斯",
    "RC Lens": "朗斯", "Lens": "朗斯", "Stade Rennais FC 1901": "雷恩", "Rennes": "雷恩",
    "RC Strasbourg Alsace": "斯特拉斯堡", "Strasbourg": "斯特拉斯堡",
    "FC Nantes": "南特", "Nantes": "南特",
    "Stade Brestois 29": "布雷斯特", "Brest": "布雷斯特",
    "Toulouse FC": "图卢兹", "Toulouse": "图卢兹",
    "AJ Auxerre": "欧塞尔", "Auxerre": "欧塞尔", "Angers SCO": "昂热", "Angers": "昂热",
    "Le Havre AC": "勒阿弗尔", "Le Havre": "勒阿弗尔",
    "FC Metz": "梅斯", "Metz": "梅斯", "Paris FC": "巴黎FC",
    "FC Lorient": "洛里昂", "Lorient": "洛里昂",
    # 荷甲 / Eredivisie（18 队）
    "AFC Ajax": "阿贾克斯", "Ajax": "阿贾克斯",
    "PSV Eindhoven": "埃因霍温", "PSV": "埃因霍温",
    "Feyenoord Rotterdam": "费耶诺德", "Feyenoord": "费耶诺德",
    "FC Utrecht": "乌得勒支", "Utrecht": "乌得勒支",
    "AZ Alkmaar": "阿尔克马尔", "AZ": "阿尔克马尔",
    "FC Twente": "特温特", "Twente": "特温特",
    "SC Heerenveen": "海伦芬", "Heerenveen": "海伦芬",
    "SBV Vitesse": "维特斯", "Vitesse": "维特斯",
    "FC Groningen": "格罗宁根", "Groningen": "格罗宁根",
    "Sparta Rotterdam": "鹿特丹斯巴达",
    "NEC Nijmegen": "奈梅亨", "NEC": "奈梅亨",
    "Go Ahead Eagles": "前进之鹰", "PEC Zwolle": "兹沃勒", "Zwolle": "兹沃勒",
    "Heracles Almelo": "阿尔梅罗大力神", "Heracles": "阿尔梅罗大力神",
    "RKC Waalwijk": "瓦尔韦克", "Willem II Tilburg": "威廉二世", "Willem II": "威廉二世",
    "NAC Breda": "布雷达", "Fortuna Sittard": "锡塔德幸运",
    "FC Volendam": "福伦丹", "Excelsior": "鹿特丹精英",
    # 葡超 / Primeira Liga（19 队）
    "SL Benfica": "本菲卡", "Benfica": "本菲卡",
    "FC Porto": "波尔图", "Porto": "波尔图",
    "Sporting CP": "葡萄牙体育", "Sporting": "葡萄牙体育",
    "SC Braga": "布拉加", "Braga": "布拉加",
    "Vitória SC": "吉马良斯", "Vitoria Guimaraes": "吉马良斯",
    "Vitória Guimarães": "吉马良斯",
    "Rio Ave FC": "里奥阿维", "Rio Ave": "里奥阿维",
    "Boavista FC": "博阿维斯塔", "Boavista": "博阿维斯塔",
    "SC Farense": "法伦斯", "Farense": "法伦斯",
    "GD Estoril Praia": "埃斯托里尔", "Estoril": "埃斯托里尔",
    "FC Famalicão": "法马利康", "Famalicao": "法马利康",
    "Moreirense FC": "莫雷伦斯", "Moreirense": "莫雷伦斯",
    "Casa Pia AC": "卡萨皮亚", "Casa Pia": "卡萨皮亚",
    "FC Arouca": "阿罗卡", "Arouca": "阿罗卡",
    "Gil Vicente FC": "吉尔维森特", "Gil Vicente": "吉尔维森特",
    "Portimonense SC": "波尔蒂芒人", "Portimonense": "波尔蒂芒人",
    "CD Santa Clara": "圣克拉拉", "Santa Clara": "圣克拉拉",
    "CF Estrela Amadora": "阿马多拉之星", "Estrela": "阿马多拉之星",
    "CD Nacional": "国民队", "Nacional": "国民队",
    "AVS Futebol SAD": "阿维斯", "AVS": "阿维斯",
    # 土超 / Süper Lig（19 队）
    "Galatasaray SK": "加拉塔萨雷", "Galatasaray": "加拉塔萨雷",
    "Fenerbahçe SK": "费内巴切", "Fenerbahce": "费内巴切",
    "Beşiktaş JK": "贝西克塔斯", "Besiktas": "贝西克塔斯",
    "Trabzonspor": "特拉布宗体育", "Başakşehir FK": "巴萨克塞希尔",
    "Istanbul Basaksehir": "巴萨克塞希尔",
    "Samsunspor": "萨姆松体育", "Konyaspor": "科尼亚体育",
    "Antalyaspor": "安塔利亚体育", "Gaziantep FK": "加济安泰普",
    "Alanyaspor": "阿拉尼亚体育", "Kayserispor": "开塞利体育",
    "Çaykur Rizespor": "里泽体育", "Rizespor": "里泽体育",
    "Kasımpaşa SK": "卡斯帕萨", "Kasimpasa": "卡斯帕萨",
    "Göztepe SK": "格兹泰佩", "Goztepe": "格兹泰佩",
    "Eyüpspor": "埃约普体育", "Eyupspor": "埃约普体育",
    "Bodrum FK": "博德鲁姆", "Hatayspor": "哈塔伊体育",
    "Adana Demirspor": "阿达纳体育", "Sivasspor": "锡瓦斯体育",
    "Fatih Karagümrük": "卡拉居姆吕克", "Kocaelispor": "科贾埃利体育",
    # 巴甲 / Brasileirão（21 队）
    "CR Flamengo": "弗拉门戈", "Flamengo": "弗拉门戈",
    "SE Palmeiras": "帕尔梅拉斯", "Palmeiras": "帕尔梅拉斯",
    "São Paulo FC": "圣保罗", "Sao Paulo": "圣保罗",
    "SC Corinthians Paulista": "科林蒂安", "Corinthians": "科林蒂安",
    "Fluminense FC": "弗卢米嫩塞", "Fluminense": "弗卢米嫩塞",
    "Santos FC": "桑托斯", "Santos": "桑托斯",
    "Grêmio FBPA": "格雷米奥", "Gremio": "格雷米奥",
    "SC Internacional": "巴西国际", "Internacional": "巴西国际",
    "CA Mineiro": "米内罗竞技", "Atletico Mineiro": "米内罗竞技",
    "Cruzeiro EC": "克鲁塞罗", "Cruzeiro": "克鲁塞罗",
    "CR Vasco da Gama": "瓦斯科达伽马", "Vasco da Gama": "瓦斯科达伽马",
    "Botafogo FR": "博塔弗戈", "Botafogo": "博塔弗戈",
    "EC Bahia": "巴伊亚", "Bahia": "巴伊亚",
    "Fortaleza EC": "福塔莱萨", "Fortaleza": "福塔莱萨",
    "CA Paranaense": "巴拉纳竞技", "Athletico Paranaense": "巴拉纳竞技",
    "RB Bragantino": "布拉甘蒂诺红牛", "Red Bull Bragantino": "布拉甘蒂诺红牛",
    "Ceará SC": "塞阿拉", "Ceara": "塞阿拉",
    "EC Juventude": "尤文图德", "Juventude": "尤文图德",
    "Sport Club do Recife": "累西腓体育", "Sport Recife": "累西腓体育",
    "Mirassol FC": "米拉索尔", "Mirassol": "米拉索尔",
    "EC Vitória": "维多利亚", "Vitoria": "维多利亚",
    "Grêmio Novorizontino": "诺瓦里桑蒂诺", "Goiás EC": "戈亚斯", "Goias": "戈亚斯",
    # —— API-Football 下发的裸名写法（无 FC/EC 词缀）——
    "Coritiba": "科里蒂巴", "Remo": "雷莫", "Chapecoense": "沙佩科恩斯",
    # 阿甲 / Liga Profesional Argentina（30 队）
    "CA River Plate": "河床", "River Plate": "河床",
    "CA Boca Juniors": "博卡青年", "Boca Juniors": "博卡青年",
    "Racing Club": "竞赛队", "CA Independiente": "独立队", "Independiente": "独立队",
    "CA San Lorenzo de Almagro": "圣洛伦索", "San Lorenzo": "圣洛伦索",
    "Estudiantes de La Plata": "拉普拉塔大学生",
    "Estudiantes de Río Cuarto": "里奥夸尔托",
    "Estudiantes de Rio Cuarto": "里奥夸尔托",
    "CA Vélez Sarsfield": "萨斯菲尔德", "Velez Sarsfield": "萨斯菲尔德",
    "Newell's Old Boys": "纽维尔老男孩", "CA Newells Old Boys": "纽维尔老男孩",
    "CA Rosario Central": "罗萨里奥中央", "Rosario Central": "罗萨里奥中央",
    "CA Talleres de Córdoba": "塔列雷斯", "Talleres Cordoba": "塔列雷斯",
    "CA Lanús": "拉努斯", "Lanus": "拉努斯",
    "CA Banfield": "班菲尔德", "Banfield": "班菲尔德",
    "CSyD Defensa y Justicia": "国防与司法", "Defensa y Justicia": "国防与司法",
    "AA Argentinos Juniors": "阿根廷青年人", "Argentinos Juniors": "阿根廷青年人",
    "CA Tigre": "老虎竞技", "Tigre": "老虎竞技",
    "CA Huracán": "飓风队", "Huracan": "飓风队",
    "CA Aldosivi": "阿尔多西维", "Aldosivi": "阿尔多西维",
    "CA Unión de Santa Fe": "圣菲联", "Union Santa Fe": "圣菲联",
    "CA Colón de Santa Fe": "圣菲科隆", "Colon Santa Fe": "圣菲科隆",
    "CD Godoy Cruz Antonio Tomba": "戈多伊克鲁斯", "Godoy Cruz": "戈多伊克鲁斯",
    "CA Platense": "普拉滕斯", "Platense": "普拉滕斯",
    "CA Central Córdoba de Santiago del Estero": "中央科尔多瓦",
    "Central Cordoba": "中央科尔多瓦",
    # 省略省份的写法 / Province dropped: 埋点实测到数据源下发
    # "Central Cordoba de Santiago"（少了 del Estero）。归一化后键是
    # "central cordoba santiago"，与上面任何一条都不同 → 落空退回英文原名。
    # 这里补上带/不带重音的常见写法，中文名相同，不构成索引冲突。
    "Central Cordoba de Santiago": "中央科尔多瓦",
    "Central Córdoba de Santiago": "中央科尔多瓦",
    "Central Córdoba": "中央科尔多瓦",
    "Instituto AC Córdoba": "科尔多瓦学院", "Instituto Cordoba": "科尔多瓦学院",
    "CA Barracas Central": "中央巴卡拉斯", "Barracas Central": "中央巴卡拉斯",
    "Deportivo Riestra": "列斯特拉", "CA Sarmiento de Junín": "萨米恩托",
    "Sarmiento Junin": "萨米恩托",
    "CA San Martín de San Juan": "圣胡安圣马丁", "San Martin San Juan": "圣胡安圣马丁",
    "Club Atlético Tucumán": "图库曼竞技", "Atletico Tucuman": "图库曼竞技",
    "Club de Gimnasia y Esgrima La Plata": "拉普拉塔体操", "Gimnasia La Plata": "拉普拉塔体操",
    "CA Belgrano de Córdoba": "贝尔格拉诺", "Belgrano": "贝尔格拉诺",
    "CA Tucumán": "图库曼竞技",
    # —— 以下为 API-Football 实际下发的缩写/短名写法 ——
    # 数据源并不总给完整名：同一支队可能下发 "Estudiantes L.P."、
    # "Argentinos Jrs"、"Ind. Rivadavia"、"Instituto" 等缩写或裸名，
    # 与上面收录的完整名对不上，会退化成英文。这里按**核实过的真实写法**
    # 补录（来源：数据源公开积分榜 / 赛事资料库），不臆造中文名。
    "Estudiantes L.P.": "拉普拉塔大学生", "Estudiantes LP": "拉普拉塔大学生",
    "Argentinos Jrs": "阿根廷青年人", "Argentinos JRS": "阿根廷青年人",
    "Independiente Rivadavia": "门多萨独立", "Ind. Rivadavia": "门多萨独立",
    "Instituto": "科尔多瓦学院", "Sarmiento": "萨米恩托",
    "Gimnasia Mendoza": "门多萨体操", "Gimnasia M.": "门多萨体操",
    "Newells OB": "纽维尔老男孩", "Dep. Riestra": "列斯特拉",
    "Atl.Tucuman": "图库曼竞技", "Atl. Tucuman": "图库曼竞技",
    "Est. Rio Cuarto": "里奥夸尔托", "Estudiantes Rio Cuarto": "里奥夸尔托",
    "Union de Santa Fe": "圣菲联", "Central Cordoba SdE": "中央科尔多瓦",
    "Defensa Jus.": "国防与司法",
    # 日职 / J1 League（20 队）
    "Vissel Kobe": "神户胜利船", "Yokohama F. Marinos": "横滨水手",
    "Yokohama F Marinos": "横滨水手",
    "Urawa Red Diamonds": "浦和红钻", "Kashima Antlers": "鹿岛鹿角",
    "Kawasaki Frontale": "川崎前锋", "FC Tokyo": "东京FC",
    "Gamba Osaka": "大阪钢巴", "Cerezo Osaka": "大阪樱花",
    "Nagoya Grampus": "名古屋鲸鱼", "Sanfrecce Hiroshima": "广岛三箭",
    "Kashiwa Reysol": "柏太阳神", "Shimizu S-Pulse": "清水心跳",
    "Avispa Fukuoka": "福冈黄蜂",
    "Hokkaido Consadole Sapporo": "札幌冈萨多", "Consadole Sapporo": "札幌冈萨多",
    "Shonan Bellmare": "湘南比马", "Albirex Niigata": "新潟天鹅",
    "Kyoto Sanga FC": "京都桑加", "Tokyo Verdy": "东京绿茵",
    "Machida Zelvia": "町田泽维亚", "Fagiano Okayama": "冈山雉鸡",
    "Kawasaki Frontale": "川崎前锋", "Yokohama FC": "横滨FC",
    "Kashiwa Reysol": "柏太阳神",
    # —— API-Football 下发的写法（Reds / V-Varen / JEF United 等）——
    "Mito Hollyhock": "水户蜀葵", "Urawa Reds": "浦和红钻",
    "V-Varen Nagasaki": "长崎航海", "JEF United Chiba": "千叶市原",
    # 韩K / K League 1（13 队）
    "Ulsan Hyundai FC": "蔚山现代", "Ulsan Hyundai": "蔚山现代",
    "Jeonbuk Hyundai Motors FC": "全北现代", "Jeonbuk Hyundai Motors": "全北现代",
    "FC Seoul": "首尔FC", "Seoul": "首尔FC",
    "Suwon Samsung Bluewings FC": "水原三星", "Suwon Samsung Bluewings": "水原三星",
    "Pohang Steelers FC": "浦项制铁", "Pohang Steelers": "浦项制铁",
    "Gimcheon Sangmu FC": "金泉尚武", "Gimcheon Sangmu": "金泉尚武",
    "Gangwon FC": "江原FC", "Daegu FC": "大邱FC", "Jeju United FC": "济州联",
    "Jeju SK FC": "济州联",
    "Daejeon Hana Citizen FC": "大田韩亚市民", "Daejeon Hana Citizen": "大田韩亚市民",
    "Gwangju FC": "光州FC", "FC Anyang": "安养", "Anyang": "安养",
    "Incheon United FC": "仁川联", "Suwon FC": "水原FC",
    # 沙特 / Saudi Pro League（20 队）
    "Al-Hilal SFC": "利雅得新月", "Al-Hilal": "利雅得新月", "Al Hilal": "利雅得新月",
    "Al-Nassr FC": "利雅得胜利", "Al-Nassr": "利雅得胜利", "Al Nassr": "利雅得胜利",
    "Al-Ittihad FC": "吉达联合", "Al-Ittihad": "吉达联合", "Al Ittihad": "吉达联合",
    "Al-Ahli SFC": "吉达国民", "Al-Ahli": "吉达国民", "Al Ahli": "吉达国民",
    "Al-Qadisiya FC": "卡迪西亚", "Al-Qadisiya": "卡迪西亚", "Al Qadisiya": "卡迪西亚",
    "Al-Shabab FC": "利雅得青年", "Al-Shabab": "利雅得青年", "Al Shabab": "利雅得青年",
    "Al-Taawoun FC": "布赖代合作", "Al-Taawoun": "布赖代合作", "Al Taawoun": "布赖代合作",
    "Al-Ettifaq FC": "达曼协作", "Al-Ettifaq": "达曼协作", "Al Ettifaq": "达曼协作",
    "Al-Fateh SC": "哈萨征服", "Al-Fateh": "哈萨征服", "Al Fateh": "哈萨征服",
    "Al-Khaleej Saihat": "赛哈特海湾", "Al-Khaleej": "赛哈特海湾",
    "Al-Fayha FC": "费哈", "Al-Fayha": "费哈", "Al Fayha": "费哈",
    "Al-Raed SC": "布赖代先锋", "Al-Raed": "布赖代先锋",
    "Al-Okhdood Club": "阿科多", "Al-Okhdood": "阿科多",
    "Al-Kholood Club": "库鲁杜德", "Al-Kholood": "库鲁杜德",
    "Al-Orobah FC": "欧鲁巴", "Al-Orobah": "欧鲁巴",
    "Al-Wehda Club": "麦加统一", "Al-Wehda": "麦加统一",
    "Al-Taee SC": "塔伊", "Al-Tai": "塔伊", "Al-Hazem SC": "哈森姆", "Al-Hazem": "哈森姆",
    "Damac FC": "达马克", "Damac": "达马克", "Al-Batin FC": "巴腾",
    "Al-Riyadh SC": "利雅得体育", "Al-Riyadh": "利雅得体育",
    # 挪超 / Eliteserien（18 队）
    "FK Bodø/Glimt": "博多格林特", "Bodo Glimt": "博多格林特", "Bodø/Glimt": "博多格林特",
    "Molde FK": "莫尔德", "Molde": "莫尔德",
    "Rosenborg BK": "罗森博格", "Rosenborg": "罗森博格",
    "Viking FK": "维京", "Viking": "维京",
    "SK Brann": "布兰", "Brann": "布兰",
    "Sarpsborg 08 FF": "萨尔普斯堡", "Sarpsborg 08": "萨尔普斯堡",
    "Strømsgodset IF": "斯特罗姆", "Stromsgodset": "斯特罗姆",
    "Vålerenga IF": "瓦勒伦加", "Valerenga": "瓦勒伦加",
    "Tromsø IL": "特罗姆瑟", "Tromso": "特罗姆瑟",
    "Lillestrøm SK": "利勒斯特罗姆", "Lillestrom": "利勒斯特罗姆",
    "Odds BK": "奥德", "Odd": "奥德",
    "Kristiansund BK": "克里斯蒂安松", "Kristiansund": "克里斯蒂安松",
    "Hamarkameratene": "哈姆卡姆", "HamKam": "哈姆卡姆",
    "Fredrikstad FK": "腓特烈斯塔", "Fredrikstad": "腓特烈斯塔",
    "FK Haugesund": "海于格松", "Haugesund": "海于格松",
    "Bryne FK": "布吕讷", "Bryne": "布吕讷",
    "Sandefjord Fotball": "桑德尔福德", "Sandefjord": "桑德尔福德",
    "KFUM Oslo": "奥斯陆KFUM",
    # 瑞超 / Allsvenskan（17 队）
    "Malmö FF": "马尔默", "Malmo FF": "马尔默",
    "AIK": "AIK索尔纳", "AIK Fotboll": "AIK索尔纳",
    "Djurgårdens IF": "尤尔加登", "Djurgardens IF": "尤尔加登",
    "IFK Göteborg": "哥德堡", "IFK Goteborg": "哥德堡",
    "Hammarby IF": "哈马比", "Hammarby": "哈马比",
    "IF Elfsborg": "埃尔夫斯堡", "Elfsborg": "埃尔夫斯堡",
    "BK Häcken": "赫根", "Hacken": "赫根",
    "Mjällby AIF": "米亚尔比", "Mjallby AIF": "米亚尔比",
    "IFK Norrköping": "北雪平", "Norrkoping": "北雪平",
    "Degerfors IF": "代格福什", "Halmstads BK": "哈尔姆斯塔德",
    "IK Sirius": "天狼星", "Sirius": "天狼星",
    "IF Brommapojkarna": "布洛马波卡纳", "Brommapojkarna": "布洛马波卡纳",
    "GAIS": "盖斯", "Östers IF": "厄斯特斯", "Osters IF": "厄斯特斯",
    "IFK Värnamo": "韦纳穆", "IFK Varnamo": "韦纳穆",
    "Örebro SK": "厄勒布鲁", "Orebro SK": "厄勒布鲁",
}


OUTCOME_LABEL = {"home": "主胜", "draw": "平局", "away": "客胜"}

# 移动端优先：43 列会在手机上折行。保留合规核心「不构成投注建议」，
# 去掉「仅基于进球数据估算」（该信息已在风险因素里逐条说明，不重复）。
DISCLAIMER = "⚠️ 仅供参考，不构成投注建议。"

VALUE_FLAG = 0.05  # 价值偏差超过此值时打 🚀 标记

VALUE_HIGH = 0.07

VALUE_LOW = 0.03

TABS = (("home", "📈 预测"), ("deep", "🔍 深度分析"), ("h2h", "📊 历史交锋"), ("odds", "💰 赔率对比"))

MENU_ITEMS = (
    ("fixtures", "📅 今日赛程"),
    ("digest", "🎯 生成今日预测"),
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
# 收起用的是 ReplyKeyboardRemove，移出的是我们的底部按钮（系统输入键盘反而会
# 回来），需要时通过「☰ 打开面板」按钮或 /panel 唤回。
# 收起态的提示图标不用 ⌨️：面板里并没有一个 ⌨️ 按键，用它做标题会让人去找。
# 这两个标签必须与 MENU_ITEMS 的文字不重合，否则 _menu_key_from_text 会把它
# 误判成菜单项。
PANEL_OPEN_LABEL = "☰ 打开面板"
PANEL_CLOSE_LABEL = "✖️ 收起面板"

PANEL_EXPANDED = "expanded"
PANEL_COLLAPSED = "collapsed"

# 收起后单独一条消息的文案：只放唤回按钮，不重复解释。
PANEL_REOPEN_TIP = "☰ 需要按钮时点这里，随时重新打开面板。"


PANEL_HINTS = {
    PANEL_EXPANDED: "⌨️ <b>功能面板已展开</b>\n直接点下方按钮开始；不需要时点「✖️ 收起面板」，把屏幕还给正文。",
    PANEL_COLLAPSED: (
        "✖️ <b>面板已收起</b>\n"
        "底部按钮已移出屏幕（系统输入键盘不受影响）。命令（如 /fixtures、/predict）照常可用；\n"
        "要按钮时点下方「☰ 打开面板」，或发送 /panel。"
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
