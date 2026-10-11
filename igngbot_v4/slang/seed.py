"""Editorial STARTER DRAFTS, not reviewed facts or current-trend assertions.

References are research starting points, NOT evidence excerpts. Import as candidates
only. An Owner must attach actual evidence and review before publication.
"""
import argparse
import json
from .models import entry

SEEDS = [
    ('yyds', 'YYDS', '永远的神的缩写，常用于夸赞，也可能是夸张反讽。'),
    ('awsl', 'AWSL', '啊我死了的拼音缩写，常夸张表达被可爱或精彩内容打动。'),
    ('xswl', 'XSWL', '笑死我了的拼音缩写，通常表达觉得好笑。'),
    ('zqsg', 'ZQSG', '真情实感的缩写，强调投入了真实情绪。'),
    ('nbcs', 'NBCS', 'Nobody cares 的缩写，常表达无人关心，语气可能不友善。'),
    ('gg', 'GG', '游戏中可指 good game；也可能调侃失败或结束，须依语境区分。'),
    ('buff', 'BUFF', '游戏中的增益效果；比喻用法指加成或附加优势。'),
    ('debuff', 'DEBUFF', '游戏中的减益效果；比喻用法指不利条件。'),
    ('afk', 'AFK', 'Away from keyboard 的缩写，常用于暂时离开或停止游玩。'),
    ('破防', '破防', '原指防御被突破；网络语境常指情绪受到触动或心态失衡。'),
    ('绷不住', '绷不住', '常指控制不住笑或情绪，也可能表达对离谱状况的无奈。'),
    ('蚌埠住了', '蚌埠住了', '绷不住了的谐音改写，通常表达忍不住笑或情绪。'),
    ('红温', '红温', '游戏和闲聊中常形容生气、急躁或情绪上头，非字面温度。'),
    ('上头', '上头', '可能指过度投入、冲动或兴奋；也有酒精等字面用法。'),
    ('下头', '下头', '可能表达好感下降、扫兴或反感，并非一般方位描述。'),
    ('逆天', '逆天', '网络评价中可能指极其离谱，也可能是夸张称赞。'),
    ('抽象', '抽象', '网络语境可形容荒诞、难理解或故意怪异的内容；也有正式学术含义。'),
    ('典', '典', '评论中的短回应可能讽刺某种观点过于典型；不能把典籍用法视为梗。'),
    ('急了', '急了', '常用于调侃对方情绪激动，也可能引发争执。'),
    ('乐', '乐', '短回应可能表示觉得好笑或嘲讽，也可能只是普通文字。'),
    ('麻了', '麻了', '常形容无奈、震惊或对反复状况失去反应，须排除身体麻木。'),
    ('寄了', '寄了', '常调侃事情失败、局面结束，须排除寄送物品。'),
    ('赢麻了', '赢麻了', '夸张表达大获成功，也经常用于反讽。'),
    ('遥遥领先', '遥遥领先', '原是领先程度描述；在特定引用中也可能是夸张称赞或反讽。'),
    ('好好好', '好好好', '重复回应可能表达认可，也可能是无奈或反讽，需看前文。'),
    ('尊嘟假嘟', '尊嘟假嘟', '真的假的的谐音表达，常带故意可爱或调侃语气。'),
    ('尊嘟', '尊嘟', '真的的谐音表达，可能是故意模仿的可爱语气。'),
    ('电子榨菜', '电子榨菜', '比喻吃饭时配着看的轻松视频、节目等内容。'),
    ('电子哈基米', '电子哈基米', '可能用于描述虚拟可爱形象；具体含义与变体需核对实际来源。'),
    ('哈基米', '哈基米', '在部分中文网络语境用于可爱动物相关内容，不能据此概括所有原始含义。'),
    ('曼波', '曼波', '可能引用音乐或二创片段，也可能指音乐舞蹈类型，需要来源消歧。'),
    ('肘击', '肘击', '有实际动作含义；部分二创语境作为固定引用，不能仅按词面判定。'),
    ('牢大', '牢大', '老大的谐音改写，在部分二创中涉及真实人物或悲剧，不建议主动使用。'),
    ('包的', '包的', '部分闲聊中表示保证、没问题；也可能只是普通语句的片段。'),
    ('包赢', '包赢', '常表达保证获胜或调侃自信，并不构成事实保证。'),
    ('水灵灵地', '水灵灵地', '在部分句式中夸张描述某件事发生，语气可能轻松或自嘲。'),
    ('硬控', '硬控', '原可指游戏的强控制；比喻用法指内容让人强烈停留或投入。'),
    ('班味', '班味', '调侃工作带来的疲惫、拘谨或精神状态，并非真的气味。'),
    ('松弛感', '松弛感', '常描述从容不紧绷的状态，具体评价依语境而定。'),
    ('情绪价值', '情绪价值', '常用于描述互动带来的情绪支持或满足，不等同于正式心理学诊断。'),
    ('搭子', '搭子', '常指针对某项活动结伴的人，如饭搭子、游戏搭子。'),
    ('显眼包', '显眼包', '常调侃特别引人注意或爱表现的人，需注意是否冒犯。'),
    ('i人', 'I人', '网络闲聊中常借 MBTI 的 I 描述内向倾向，不能作为人格诊断。'),
    ('e人', 'E人', '网络闲聊中常借 MBTI 的 E 描述外向倾向，不能作为人格诊断。'),
    ('内卷', '内卷', '网络语境常指竞争投入不断增加但收益有限，不能替代原学术含义。'),
    ('躺平', '躺平', '可指降低竞争投入或选择不争，也可能只是实际躺下。'),
    ('摆烂', '摆烂', '常指不再努力改善不利局面，可能是自嘲而非真实决定。'),
    ('摸鱼', '摸鱼', '常指工作或任务期间偷闲，须排除真实捕鱼。'),
    ('社死', '社死', '常夸张表达在社交场合尴尬，不应当作真实死亡。'),
    ('社牛', '社牛', '常调侃社交表现大胆或自如，不是医学概念。'),
    ('天花板', '天花板', '比喻用法可能指某领域顶尖水平，须排除建筑含义。'),
    ('地板', '地板', '评价语境可能比喻很低的水平，须排除建筑含义。'),
    ('小丑', '小丑', '网络语境可能是自嘲或贬损称呼，也有表演角色含义。'),
    ('凡尔赛', '凡尔赛', '网络语境可指以抱怨或谦虚形式展示优越感，也可能指法国地点。'),
    ('爷青回', '爷青回', '爷的青春回来了的缩写，表达怀旧或重逢旧内容。'),
    ('爷青结', '爷青结', '爷的青春结束了的缩写，夸张表达旧内容变化或结束。'),
    ('真香', '真香', '可能引用先拒绝后接受的反转，也有气味字面含义。'),
    ('夺笋', '夺笋', '多损的谐音，常调侃损人或尖锐玩笑，需注意对象感受。'),
    ('绝绝子', '绝绝子', '夸张评价表达，可能称赞也可能反讽，需看语境。'),
    ('栓Q', '栓Q', 'Thank you 的谐音写法，部分用法表达无奈或反讽。'),
]


def entries():
    drafts = []
    for index, (key, name, meaning) in enumerate(SEEDS):
        needs = len(name) < 3 or key in {'上头', '下头', '抽象', '摸鱼', '天花板', '地板', '凡尔赛', '真香', '小丑', '遥遥领先'}
        aliases = [{'text': name, 'requires_any': ['聊天', '游戏', '评论', '调侃'] if needs else [],
                    'excludes_any': ['典籍', '建筑', '快递', '身体麻木'] if needs else []}]
        risks = ['tragedy', 'controversial'] if key == '牢大' else ['harassment'] if key in {'急了', '小丑', 'nbcs'} else []
        drafts.append(entry({'id': f'seed:{index:02d}', 'name': name, 'meaning': meaning, 'aliases': aliases,
                             'contexts': ['casual', 'gaming'], 'topics': ['聊天', '游戏'], 'usage': 'understand_only',
                             'lifecycle': 'unknown', 'confidence': 'low', 'risks': risks,
                             'sources': [{'id': f'research:{index:02d}', 'url': 'https://zh.wikipedia.org/wiki/网络流行语',
                                          'kind': 'reference', 'verified': False, 'excerpt': '', 'origin': 'editorial-research-start'}]}))
    return drafts


def evaluation():
    # 200 deterministic engineering cases, NOT a human blind evaluation or trend corpus.
    fixtures, cases = [], []
    for value in entries()[:50]:
        fixture = {**value, 'status': 'published', 'confidence': 'high', 'version': 1,
                   'sources': [{'id': 'fixture:source', 'url': 'https://example.com/fixture', 'verified': True, 'excerpt': '合成测试证据；不代表已经审核真实释义。'}]}
        fixtures.append(fixture)
        name = value['name']
        cases.extend([
            {'id': value['id'] + ':exact', 'text': '聊天评论：' + name, 'expected': [value['id']], 'kind': 'literal'},
            {'id': value['id'] + ':variant', 'text': '游戏聊天，' + name.lower(), 'expected': [value['id']], 'kind': 'normalized'},
            {'id': value['id'] + ':negative', 'text': '今天查询普通数据库记录编号' + value['id'], 'expected': [], 'kind': 'negative'},
            {'id': value['id'] + ':unknown', 'text': '还没有收录的合成表达编号' + value['id'], 'expected': [], 'kind': 'unknown'},
        ])
    return {'warning': 'synthetic engineering cases only; human review and real-model blind evaluation pending', 'entries': fixtures, 'cases': cases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evaluation', action='store_true')
    args = parser.parse_args()
    print(json.dumps(evaluation() if args.evaluation else {'status': 'candidate', 'entries': entries()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
