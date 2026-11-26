"""Value pools and phrasing templates for the synthetic personas.

Every value is (English, Chinese, *extra aliases). Templates use {v} for the value in the conversation's language,
{old} for the value it replaces, {d} for a distractor value, and slot-specific fields ({species}, {sib}, {proj}).

The pools deliberately avoid anything from the real author's profile (school, city, courses, names, projects), so a
leak from a model's own context can be told apart from what the synthetic history says.
"""

CITIES = [  # (en, zh, country_en, country_zh)
    ("Vancouver", "温哥华", "Canada", "加拿大"), ("Montreal", "蒙特利尔", "Canada", "加拿大"),
    ("Berlin", "柏林", "Germany", "德国"), ("Munich", "慕尼黑", "Germany", "德国"),
    ("Lisbon", "里斯本", "Portugal", "葡萄牙"), ("Seattle", "西雅图", "United States", "美国"),
    ("Austin", "奥斯汀", "United States", "美国"), ("Melbourne", "墨尔本", "Australia", "澳大利亚"),
    ("Osaka", "大阪", "Japan", "日本"), ("Seoul", "首尔", "South Korea", "韩国"),
    ("Chengdu", "成都", "China", "中国"), ("Hangzhou", "杭州", "China", "中国"), ("Shenzhen", "深圳", "China", "中国"),
    ("Edinburgh", "爱丁堡", "United Kingdom", "英国"), ("Dublin", "都柏林", "Ireland", "爱尔兰"),
    ("Amsterdam", "阿姆斯特丹", "Netherlands", "荷兰"), ("Zurich", "苏黎世", "Switzerland", "瑞士"),
    ("Wellington", "惠灵顿", "New Zealand", "新西兰"),
]
COUNTRY_ALIASES = {"United States": ["USA", "US", "America", "美国"], "United Kingdom": ["UK", "Britain", "英国"],
                   "South Korea": ["Korea", "韩国"]}
HOMETOWNS = [("Xiamen", "厦门"), ("Kunming", "昆明"), ("Harbin", "哈尔滨"), ("Qingdao", "青岛"), ("Changsha", "长沙"),
             ("Lanzhou", "兰州"), ("Wuxi", "无锡"), ("Guilin", "桂林"), ("Dalian", "大连"), ("Luoyang", "洛阳")]
NAMES = [("Chen Yiran", "陈怡然", "Yiran"), ("Zhou Minghao", "周明浩", "Minghao"), ("Lin Shuyao", "林书瑶", "Shuyao"),
         ("He Zixuan", "何梓轩", "Zixuan"), ("Xu Jingwen", "许静雯", "Jingwen"), ("Luo Haoran", "罗浩然", "Haoran"),
         ("Song Qinghe", "宋清禾", "Qinghe"), ("Fang Yuchen", "方宇辰", "Yuchen"), ("Tang Ruoxi", "唐若溪", "Ruoxi"),
         ("Gao Zhiyuan", "高致远", "Zhiyuan"), ("Ma Lanxin", "马岚欣", "Lanxin"), ("Ye Chengze", "叶承泽", "Chengze")]
UNIVERSITIES = [("McGill University", "麦吉尔大学", "McGill"), ("UBC", "英属哥伦比亚大学", "University of British Columbia"),
                ("University of Edinburgh", "爱丁堡大学", "Edinburgh"), ("TU Munich", "慕尼黑工业大学", "TUM"),
                ("Zhejiang University", "浙江大学", "浙大"), ("Wuhan University", "武汉大学", "武大"),
                ("University of Melbourne", "墨尔本大学", "Melbourne"), ("Kyoto University", "京都大学", "Kyoto"),
                ("Sichuan University", "四川大学", "川大"), ("University of Washington", "华盛顿大学", "UW")]
MAJORS = [("physics", "物理"), ("economics", "经济学", "经济"), ("mechanical engineering", "机械工程"),
          ("biology", "生物", "生物学"), ("architecture", "建筑", "建筑学"), ("linguistics", "语言学"),
          ("chemistry", "化学"), ("statistics", "统计学", "统计")]
PET_NAMES = [("Mochi", "Mochi", "麻薯"), ("Biscuit", "Biscuit", "饼干"), ("Pepper", "Pepper"), ("Nugget", "Nugget"),
             ("Luna", "Luna"), ("Miso", "Miso", "味噌"), ("Pickles", "Pickles"), ("Juniper", "Juniper"),
             ("Waffle", "Waffle"), ("Kiwi", "Kiwi")]
SPECIES = [("cat", "猫"), ("dog", "狗"), ("rabbit", "兔子"), ("parrot", "鹦鹉")]
SIBLINGS = [("Anna", "Anna"), ("Vivian", "Vivian"), ("Iris", "Iris"), ("Olivia", "Olivia"), ("Hana", "Hana"),
            ("Clara", "Clara"), ("Nora", "Nora"), ("Ruby", "Ruby")]
SIBLING_JOBS = [("dentist", "牙医"), ("pilot", "飞行员"), ("nurse", "护士"), ("lawyer", "律师"), ("chef", "厨师"),
                ("firefighter", "消防员"), ("pharmacist", "药剂师"), ("high school teacher", "中学老师", "teacher", "老师"),
                ("veterinarian", "兽医", "vet")]
ALLERGIES = [("peanuts", "花生", "peanut"), ("shrimp", "虾"), ("pollen", "花粉"), ("penicillin", "青霉素"),
             ("mango", "芒果", "mangoes"), ("dust mites", "尘螨"), ("eggs", "鸡蛋", "egg")]
INSTRUMENTS = [("piano", "钢琴"), ("violin", "小提琴"), ("guitar", "吉他"), ("cello", "大提琴"), ("drums", "架子鼓", "鼓"),
               ("guzheng", "古筝"), ("flute", "长笛"), ("saxophone", "萨克斯", "sax")]
FOODS = [("hotpot", "火锅"), ("ramen", "拉面"), ("tacos", "塔可", "taco"), ("sushi", "寿司"), ("pho", "越南河粉", "河粉"),
         ("mapo tofu", "麻婆豆腐"), ("croissants", "可颂", "croissant"), ("biryani", "印度香饭"), ("laksa", "叻沙")]
OSES = [("macOS", "macOS", "Mac"), ("Windows", "Windows"), ("Ubuntu", "Ubuntu"), ("Fedora", "Fedora"),
        ("Arch Linux", "Arch Linux", "Arch")]
EMPLOYERS = [(n, n) for n in ["Northwind Labs", "Bluefin Analytics", "Quillstone", "Harbor & Pine", "Corvid Robotics",
                              "Lattice Health", "Tidewater Games", "Orbitly", "Fernhill Bank", "Sablewood Media",
                              "Kitebird", "Granite Logistics"]]
ROLES = [("data analyst", "数据分析师"), ("backend engineer", "后端工程师", "后端开发"), ("product manager", "产品经理", "PM"),
         ("UX designer", "交互设计师", "UX 设计师"), ("QA engineer", "测试工程师", "QA"),
         ("ML engineer", "机器学习工程师", "machine learning engineer"), ("mobile developer", "移动端开发", "移动开发"),
         ("technical writer", "技术文档工程师"), ("research assistant", "研究助理"), ("frontend engineer", "前端工程师")]
EDITORS = [(n, n) for n in ["VS Code", "Vim", "Neovim", "Emacs", "PyCharm", "Zed", "Sublime Text", "IntelliJ IDEA",
                            "Cursor"]]
PROJECTS = [(n, n) for n in ["Kestrel", "MapleNote", "Driftwood", "PaperCrane", "Tallow", "Lumen Tracker", "Quokka",
                             "Starling", "Pebble Budget", "Saffron"]]
SPORTS = [("swimming", "游泳"), ("rock climbing", "攀岩"), ("badminton", "羽毛球"), ("running", "跑步"), ("yoga", "瑜伽"),
          ("tennis", "网球"), ("cycling", "骑行"), ("boxing", "拳击"), ("table tennis", "乒乓球", "ping pong")]
STYLES = [("concise answers", "简洁的回答", "concise", "简洁"),
          ("step-by-step explanations", "一步一步的详细解释", "step by step", "一步一步"),
          ("bullet points", "分点列出", "bulleted", "分点"),
          ("code examples first", "先给代码示例", "code first", "先给代码"),
          ("short answers with sources", "简短并附上出处", "with sources", "附上出处")]
REPLY_LANGS = [("Chinese", "中文"), ("English", "英文", "英语"),
               ("Chinese with English technical terms", "中文，技术名词保留英文", "技术名词保留英文")]
LANGS = [(n, n) for n in ["Go", "Rust", "TypeScript", "Java", "Kotlin", "Swift", "C++", "Ruby", "Elixir", "Scala",
                          "Haskell", "Julia", "Python", "R"]]
COUNTRIES = [("Japan", "日本"), ("Iceland", "冰岛"), ("Peru", "秘鲁"), ("Morocco", "摩洛哥"), ("Vietnam", "越南"),
             ("Norway", "挪威"), ("Mexico", "墨西哥"), ("Egypt", "埃及"), ("Greece", "希腊"), ("Thailand", "泰国"),
             ("Kenya", "肯尼亚"), ("Chile", "智利"), ("Turkey", "土耳其")]

# kind: static | update | set.  channel: chat (only chat apps) | any (coding agents too)
SLOTS = {
    "name": dict(kind="static", channel="chat", pool=NAMES, absent_ok=False,
                 zh=["我叫{v}。", "对了，我的名字是{v}，署名的时候用这个。", "帮我写个自我介绍，开头就说我叫{v}。"],
                 en=["My name is {v}.", "Sign it as {v}, that's my name.", "Write a short intro that starts with 'Hi, I'm {v}'."],
                 dzh=["我同事也叫{d}，经常有人搞混。"], den=["My manager's name is {d}, by the way, if you need it for the email."],
                 q=("我叫什么名字？", "What is my name?")),
    "hometown": dict(kind="static", channel="chat", pool=HOMETOWNS,
                     zh=["我老家是{v}的。", "过年要回{v}老家，帮我列个带回去的东西清单。", "我是在{v}长大的。"],
                     en=["I grew up in {v}.", "I'm flying back to {v}, my hometown, for the holidays.",
                         "Back home in {v} we used to eat this all the time."],
                     dzh=["我室友是{d}人，他推荐了几个菜。", "我从来没去过{d}，听说挺好玩的。"],
                     den=["My roommate is from {d} and keeps recommending dishes.", "I've never been to {d}."],
                     q=("我的老家在哪里？", "Where is my hometown?")),
    "university": dict(kind="static", channel="chat", pool=UNIVERSITIES,
                       zh=["我是{v}毕业的。", "我本科在{v}读的。"],
                       en=["I graduated from {v}.", "Back when I was an undergrad at {v}, we had a course like this."],
                       dzh=["我表弟想申请{d}，你觉得难吗？"], den=["My cousin is applying to {d}; how competitive is it?"],
                       q=("我是哪所大学毕业的？", "Which university did I graduate from?")),
    "major": dict(kind="static", channel="chat", pool=MAJORS,
                  zh=["我大学学的是{v}。", "我本科专业是{v}，所以这块不太熟。"],
                  en=["I studied {v} in college.", "My degree is in {v}, so this area is new to me."],
                  dzh=["我女朋友是学{d}的，她说这个公式不对。"], den=["My partner studied {d} and says this formula is off."],
                  q=("我大学读的是什么专业？", "What did I study at university?")),
    "pet": dict(kind="static", channel="chat", pool=PET_NAMES,
                zh=["我家{species}叫{v}。", "{v}（我养的{species}）今天又把杯子打翻了。", "帮我查查{species}能不能吃葡萄，我怕{v}偷吃。"],
                en=["My {species} is called {v}.", "{v}, my {species}, knocked my mug over again.",
                    "Can {species}s eat grapes? I'm worried {v} will steal some."],
                dzh=["邻居家的{species}叫{d}，特别吵。"], den=["My neighbour's {species} {d} barks all night."],
                q=("我养的宠物叫什么名字？", "What is my pet's name?")),
    "sibling": dict(kind="static", channel="chat", pool=SIBLINGS,
                    zh=["我姐姐叫{v}。", "下周是我姐{v}的生日，帮我想想送什么。"],
                    en=["My sister's name is {v}.", "It's my sister {v}'s birthday next week, any gift ideas?"],
                    dzh=["我同事{d}是独生女。"], den=["My coworker {d} is an only child."],
                    q=("我姐姐叫什么名字？", "What is my sister's name?")),
    "sibling_job": dict(kind="static", channel="chat", pool=SIBLING_JOBS, needs="sibling",
                        zh=["{sib}最近刚当上{v}，忙得不行。", "{sib}说当{v}压力挺大的，帮我想几句鼓励她的话。"],
                        en=["{sib} just started working as a {v} and is swamped.",
                            "{sib} says being a {v} is stressful; help me write something encouraging."],
                        dzh=["我朋友是{d}，说这行很累。"], den=["A friend of mine is a {d} and says it's exhausting."],
                        q=("我姐姐是做什么工作的？", "What does my sister do for work?")),
    "allergy": dict(kind="static", channel="chat", pool=ALLERGIES,
                    zh=["我对{v}过敏，菜谱里别放。", "推荐几道菜吧，记得我{v}过敏。"],
                    en=["I'm allergic to {v}, so leave it out of the recipe.", "Suggest some dishes, keeping in mind my {v} allergy."],
                    dzh=["我同事对{d}过敏，聚餐要注意什么？"], den=["A coworker is allergic to {d}; what should we avoid at the party?"],
                    q=("我对什么过敏？", "What am I allergic to?")),
    "instrument": dict(kind="static", channel="chat", pool=INSTRUMENTS,
                       zh=["我学了好几年{v}。", "周末练{v}的时候手腕有点疼，怎么办？"],
                       en=["I've played the {v} for years.", "My wrist hurts after practising {v} on weekends; any tips?"],
                       dzh=["我一直想学{d}，但没时间。"], den=["I've always wanted to learn {d} but never had time."],
                       q=("我会演奏什么乐器？", "What instrument do I play?")),
    "favorite_food": dict(kind="static", channel="chat", pool=FOODS,
                          zh=["我最爱吃{v}。", "我可以天天吃{v}，帮我找个在家做的方法。"],
                          en=["My favourite food is {v}.", "I could eat {v} every day; how do I make it at home?"],
                          dzh=["我室友特别喜欢{d}，我其实一般。"], den=["My roommate loves {d}; I'm lukewarm on it."],
                          q=("我最喜欢吃什么？", "What is my favourite food?")),
    "os": dict(kind="static", channel="any", pool=OSES,
               zh=["我电脑是{v}系统。", "我用的是{v}，这个命令要怎么改？"],
               en=["I'm on {v}.", "I use {v}; how do I adapt this command?"],
               dzh=["服务器是{d}，但我自己不用。"], den=["The server runs {d}, but I don't use it myself."],
               q=("我平时用什么操作系统？", "What operating system do I use?")),
    "city": dict(kind="update", channel="chat", pool=CITIES, max_changes=2,
                 zh=["我住在{v}。", "{v}今天又下雨了，周末有什么室内活动推荐？", "我在{v}，帮我查一下附近的周末活动。"],
                 en=["I live in {v}.", "It's raining again here in {v}; any indoor ideas for the weekend?",
                     "I'm based in {v}; what's on this weekend?"],
                 uzh=["我上个月从{old}搬到{v}了，帮我列个搬家后要办的事。", "已经从{old}搬到{v}了，这边房租贵多了。"],
                 uen=["I just moved from {old} to {v}; what admin should I sort out?", "Finished moving from {old} to {v}. Rent is way higher here."],
                 dzh=["如果我搬去{d}，生活成本会高多少？", "我朋友住在{d}，下个月去看他。"],
                 den=["If I moved to {d}, how much would my cost of living change?", "A friend lives in {d}; I'm visiting next month."],
                 q=("我现在住在哪个城市？", "Which city do I live in now?"),
                 qprev=("我搬到{v}之前住在哪个城市？", "Which city did I live in before moving to {v}?")),
    "employer": dict(kind="update", channel="any", pool=EMPLOYERS, max_changes=2,
                     zh=["我在{v}上班。", "我们公司{v}要求代码必须有单测。"],
                     en=["I work at {v}.", "At {v} every change needs a unit test."],
                     uzh=["我从{old}离职了，下周开始去{v}上班。"], uen=["I left {old}; I start at {v} next week."],
                     dzh=["{d}那边在招人，你觉得值得投吗？"], den=["{d} is hiring; is it worth applying?"],
                     q=("我现在在哪家公司工作？", "Which company do I work for now?"),
                     qprev=("我去{v}之前在哪家公司工作？", "Where did I work before joining {v}?")),
    "role": dict(kind="update", channel="any", pool=ROLES,
                 zh=["我是{v}。", "作为{v}，我需要每周写周报，帮我搭个模板。"],
                 en=["I'm a {v}.", "As a {v} I write a weekly report; help me draft a template."],
                 uzh=["我从{old}转岗成{v}了，要补哪些技能？"], uen=["I switched from {old} to {v}; what should I learn first?"],
                 dzh=["我在考虑以后转做{d}，前景怎么样？"], den=["I'm considering becoming a {d} someday; what's the outlook?"],
                 q=("我现在的职位是什么？", "What is my current job title?"),
                 qprev=("我当{v}之前是什么职位？", "What was my role before I became a {v}?")),
    "editor": dict(kind="update", channel="any", pool=EDITORS,
                   zh=["我用{v}写代码。", "{v}里怎么设置自动格式化？"],
                   en=["I code in {v}.", "How do I set up format-on-save in {v}?"],
                   uzh=["我从{old}换到{v}了，快捷键还不习惯。"], uen=["I switched from {old} to {v}; still learning the shortcuts."],
                   dzh=["同事一直安利{d}，它比我现在用的好在哪？"], den=["A colleague keeps pushing {d}; is it really better?"],
                   q=("我现在用什么编辑器写代码？", "Which code editor do I use now?"),
                   qprev=("我换成{v}之前用的是什么编辑器？", "Which editor did I use before switching to {v}?")),
    "project": dict(kind="update", channel="any", pool=PROJECTS,
                    zh=["我在做一个叫{v}的项目。", "{v}这个项目的部署脚本老出错。"],
                    en=["I'm building a project called {v}.", "The deploy script for {v} keeps failing."],
                    uzh=["{old}先放一放了，现在主要精力在新项目{v}上。"], uen=["I've shelved {old}; my main project is now {v}."],
                    dzh=["看到一个开源项目叫{d}，挺有意思。"], den=["Saw an open-source project called {d}; looks neat."],
                    q=("我现在主要在做哪个项目？", "What is my main project right now?"),
                    qprev=("我做{v}之前的主要项目叫什么？", "What was my main project before {v}?")),
    "sport": dict(kind="update", channel="chat", pool=SPORTS,
                  zh=["我每周都去{v}。", "{v}完总是腿酸，怎么拉伸？"],
                  en=["I go {v} every week.", "My legs are sore after {v}; what stretches help?"],
                  uzh=["最近不{old}了，改成{v}。"], uen=["I've stopped {old} and taken up {v} instead."],
                  dzh=["我妈最近迷上了{d}。"], den=["My mum just got into {d}."],
                  q=("我现在常做什么运动？", "What sport do I do regularly now?"),
                  qprev=("我改成{v}之前常做什么运动？", "What sport did I do before taking up {v}?")),
    "answer_style": dict(kind="update", channel="any", pool=STYLES, qtype="preference",
                         zh=["以后请给我{v}。", "我比较喜欢{v}。"],
                         en=["Please give me {v} from now on.", "I prefer {v}."],
                         uzh=["之前说要{old}，现在改一下，我更想要{v}。"], uen=["Earlier I asked for {old}; I'd now rather have {v}."],
                         dzh=[], den=[], q=("我希望你用什么风格回答我？", "What answer style do I want from you?")),
    "reply_language": dict(kind="static", channel="any", pool=REPLY_LANGS, qtype="preference", absent_ok=False,
                           zh=["回答请用{v}。"], en=["Please reply in {v}."], dzh=[], den=[],
                           q=("我希望你用什么语言回复我？", "Which language do I want replies in?")),
    "prog_langs": dict(kind="set", channel="any", pool=LANGS, size=(2, 5),
                       zh=["我写过不少{v}。", "我工作里主要用{v}。", "我用{v}写了个小工具，帮我看看结构。"],
                       en=["I've written a lot of {v}.", "I use {v} at work.", "I wrote a small tool in {v}; can you review the layout?"],
                       dzh=["{d}值得学吗？我还没碰过。"], den=["Is {d} worth learning? I've never touched it."],
                       q=("我用过哪些编程语言？", "Which programming languages have I used?"),
                       qcount=("我提到自己用过几种编程语言？", "How many programming languages have I said I use?")),
    "countries": dict(kind="set", channel="chat", pool=COUNTRIES, size=(2, 5),
                      zh=["我去年去了{v}旅行。", "在{v}旅行的时候吃坏了肚子，到现在还记得。"],
                      en=["I travelled to {v} last year.", "I got food poisoning when I was travelling in {v}."],
                      dzh=["我一直想去{d}，还没去成。", "我朋友刚从{d}回来。"],
                      den=["I've always wanted to visit {d} but haven't yet.", "A friend just got back from {d}."],
                      q=("我去过哪些国家旅行？", "Which countries have I travelled to?"),
                      qcount=("我提到自己去过几个国家？", "How many countries have I said I've visited?")),
}
PROJECT_LANG = dict(zh=["{proj}的后端是用{v}写的。", "{proj}用的是{v}，编译有点慢。"],
                    en=["The backend of {proj} is written in {v}.", "{proj} is in {v}, and builds are slow."])
MULTI_HOP_Q = {
    "city_country": ("我现在住的城市在哪个国家？", "Which country is the city I live in now in?"),
    "sibling_job": ("我姐姐是做什么工作的？", "What does my sister do for work?"),
    "project_lang": ("我现在的主要项目是用什么编程语言写的？", "What programming language is my current main project written in?"),
}

# Filler: requests that state nothing about the user (no places, languages, jobs or habits).
TOOLS = ["React", "PostgreSQL", "Redis", "Docker", "Kubernetes", "Django", "Flask", "GraphQL", "Kafka", "Nginx",
         "SQLite", "Terraform"]
CODE_ZH = ["这个报错怎么解决？", "帮我解释一下这段代码在做什么", "{t} 和 {u} 有什么区别？", "怎么用 {t} 实现分页查询",
           "为什么 {t} 启动很慢", "能不能帮我优化一下这个 SQL", "写一个 {t} 的单元测试", "这段代码有内存泄漏吗",
           "数据库索引应该怎么设计", "如何在 {t} 里处理并发"]
CODE_EN = ["Why does {t} throw this error?", "How do I paginate results in {t}?", "What's the difference between {t} and {u}?",
           "Can you refactor this function?", "Write a unit test for this {t} module", "Is there a memory leak here?",
           "How do I speed up this query?", "What does this stack trace mean?"]
GEN_ZH = ["帮我写一封请假邮件", "推荐几本科幻小说", "番茄炒蛋怎么做比较好吃", "解释一下通货膨胀是怎么回事", "帮我润色这段话",
          "怎么提高睡眠质量", "给我出几道逻辑题", "怎么写一份好的会议纪要", "帮我把这段话翻译成英文", "有什么高效记笔记的方法"]
GEN_EN = ["Draft a polite email asking to move a meeting", "Recommend a few science fiction novels",
          "Explain how compound interest works", "Proofread this paragraph", "How can I sleep better?",
          "Give me three logic puzzles", "How do I write good meeting notes?", "Summarise the causes of the French Revolution",
          "What's a good way to take notes in lectures?"]
ANS_ZH = ["首先需要确认配置是否正确，然后检查日志中的错误信息。", "这里的关键是理解数据是如何在组件之间流动的。",
          "可以考虑使用缓存来减少数据库的查询次数。", "建议把这个函数拆分成更小的部分，便于测试和维护。",
          "可以分三步来做：先列出目标，再拆分任务，最后定一个检查时间。", "这个问题有几种常见的思路，下面分别说明。",
          "整体来说，保持简单、一致比追求完美更重要。", "下面是一个可以直接使用的版本，你可以按需修改。"]
ANS_EN = ["First check the configuration, then look at the logs for the actual error.",
          "The key is understanding how data flows between the components.",
          "Consider caching to cut round trips, and plan how the cache is invalidated.",
          "Split this into smaller pieces so each can be tested on its own.",
          "There are a few common approaches; here is how they compare.",
          "Here is a version you can use as-is and adjust as needed.",
          "In short: keep it simple and consistent rather than perfect."]
CODE_LINES = ["def handler(event, context):", "    items = load_items(event['ids'])", "    for item in items:",
              "        if item.status == 'ready':", "            process(item)", "    return {'ok': True}",
              "const res = await fetch(url, { method: 'POST' });", "SELECT id, name FROM users WHERE created_at > ?;",
              "Traceback (most recent call last):", "ERROR connection refused", "    at Object.<anonymous> (/srv/index.js:12:5)"]
