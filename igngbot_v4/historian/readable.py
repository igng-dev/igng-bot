"""Plain-Chinese event lines, the slim model view and the v2 weekly report contract.

The model reads one server-written Chinese line per event instead of raw payload JSON,
so its prose doesn't inherit UUIDs, English codes or "the event shows" phrasing.
"""
import re
from datetime import timezone
from zoneinfo import ZoneInfo
from .events import instant

VIEW_VERSION = 2
REPORT_VERSION = 2
# 5-minute position pings drive online-time statistics only; they are not stored for the model.
MODEL_HIDDEN_TYPES = frozenset({'PRESENCE'})
WEEKDAYS = '一二三四五六日'

WORLDS = {'world': '主世界', 'world_nether': '下界', 'world_the_end': '末地'}

QUIT_REASONS = {'DISCONNECTED': '', 'TIMED_OUT': '连接超时', 'ERRONEOUS_STATE': '异常断开', 'KICKED': '被踢出'}

KICK_CAUSES = {
    'PLUGIN': '插件踢出', 'WHITELIST': '不在白名单', 'BANNED': '被封禁', 'IP_BANNED': 'IP 被封禁',
    'KICK_COMMAND': '管理员踢出', 'FLYING_PLAYER': '疑似飞行', 'FLYING_VEHICLE': '载具疑似飞行',
    'TIMEOUT': '连接超时', 'IDLING': '挂机过久', 'INVALID_VEHICLE_MOVEMENT': '载具移动异常',
    'INVALID_PLAYER_MOVEMENT': '移动异常', 'INVALID_ENTITY_ATTACKED': '攻击无效实体', 'INVALID_PAYLOAD': '数据包异常',
    'SPAM': '刷屏', 'ILLEGAL_ACTION': '非法操作', 'ILLEGAL_CHARACTERS': '聊天含非法字符',
    'OUT_OF_ORDER_CHAT': '聊天顺序异常', 'UNSIGNED_CHAT': '聊天未签名', 'CHAT_VALIDATION_FAILED': '聊天校验失败',
    'TOO_MANY_PENDING_CHATS': '聊天积压过多', 'SELF_INTERACTION': '与自己交互', 'DUPLICATE_LOGIN': '同一账号在别处登录',
    'RESOURCE_PACK_REJECTION': '拒绝资源包', 'INVALID_COOKIE': '登录凭据无效', 'RESTART_COMMAND': '服务器重启',
    'TRANSFER': '转服', 'UNKNOWN': '原因未知'}

ENTITIES = {
    'ZOMBIE': '僵尸', 'ZOMBIE_VILLAGER': '僵尸村民', 'HUSK': '尸壳', 'DROWNED': '溺尸', 'SKELETON': '骷髅',
    'STRAY': '流浪者', 'BOGGED': '沼骸', 'WITHER_SKELETON': '凋灵骷髅', 'CREEPER': '苦力怕', 'SPIDER': '蜘蛛',
    'CAVE_SPIDER': '洞穴蜘蛛', 'ENDERMAN': '末影人', 'ENDERMITE': '末影螨', 'SILVERFISH': '蠹虫', 'WITCH': '女巫',
    'SLIME': '史莱姆', 'MAGMA_CUBE': '岩浆怪', 'BLAZE': '烈焰人', 'GHAST': '恶魂', 'PIGLIN': '猪灵',
    'PIGLIN_BRUTE': '猪灵蛮兵', 'ZOMBIFIED_PIGLIN': '僵尸猪灵', 'PIG_ZOMBIE': '僵尸猪灵', 'HOGLIN': '疣猪兽',
    'ZOGLIN': '僵尸疣猪兽', 'PHANTOM': '幻翼', 'GUARDIAN': '守卫者', 'ELDER_GUARDIAN': '远古守卫者',
    'SHULKER': '潜影贝', 'VINDICATOR': '卫道士', 'EVOKER': '唤魔者', 'PILLAGER': '掠夺者', 'RAVAGER': '劫掠兽',
    'VEX': '恼鬼', 'ILLUSIONER': '幻术师', 'WARDEN': '监守者', 'BREEZE': '旋风人', 'CREAKING': '嘎枝',
    'ENDER_DRAGON': '末影龙', 'WITHER': '凋灵', 'IRON_GOLEM': '铁傀儡', 'SNOW_GOLEM': '雪傀儡', 'WOLF': '狼',
    'BEE': '蜜蜂', 'POLAR_BEAR': '北极熊', 'LLAMA': '羊驼', 'TRADER_LLAMA': '行商羊驼', 'GOAT': '山羊',
    'PUFFERFISH': '河豚', 'DOLPHIN': '海豚', 'PANDA': '熊猫', 'FOX': '狐狸', 'AXOLOTL': '美西螈', 'HORSE': '马',
    'CAMEL': '骆驼', 'VILLAGER': '村民', 'WANDERING_TRADER': '流浪商人', 'PLAYER': '玩家', 'ARROW': '箭',
    'SPECTRAL_ARROW': '光灵箭', 'TRIDENT': '三叉戟', 'TNT': 'TNT', 'PRIMED_TNT': 'TNT', 'TNT_MINECART': 'TNT 矿车',
    'MINECART_TNT': 'TNT 矿车', 'FIREBALL': '火球', 'SMALL_FIREBALL': '小火球', 'DRAGON_FIREBALL': '末影龙火球',
    'WITHER_SKULL': '凋灵之首', 'LIGHTNING_BOLT': '闪电', 'LIGHTNING': '闪电', 'FALLING_BLOCK': '掉落的方块',
    'END_CRYSTAL': '末地水晶', 'ENDER_CRYSTAL': '末地水晶', 'FIREWORK_ROCKET': '烟花火箭', 'FIREWORK': '烟花火箭',
    'AREA_EFFECT_CLOUD': '药水云', 'POTION': '药水', 'SPLASH_POTION': '喷溅药水', 'LLAMA_SPIT': '羊驼口水',
    'SHULKER_BULLET': '潜影弹', 'EVOKER_FANGS': '唤魔者尖牙', 'WIND_CHARGE': '风弹', 'BREEZE_WIND_CHARGE': '风弹',
    'SNOWBALL': '雪球', 'EGG': '鸡蛋', 'ENDER_PEARL': '末影珍珠', 'ARMOR_STAND': '盔甲架', 'BOAT': '船',
    'MINECART': '矿车'}

# Vanilla damage type keys (namespace stripped) → "<player> <phrase>".
DAMAGE_TYPES = {
    'in_fire': '被火烧死', 'campfire': '被营火烧死', 'on_fire': '被烧死', 'lava': '掉进岩浆烧死',
    'hot_floor': '被岩浆块烫死', 'lightning_bolt': '被闪电劈死', 'in_wall': '卡在方块里窒息而死',
    'cramming': '被挤压致死', 'drown': '淹死', 'starve': '饿死', 'cactus': '被仙人掌扎死', 'fall': '摔死',
    'ender_pearl': '扔末影珍珠摔死', 'fly_into_wall': '撞墙而死', 'out_of_world': '掉出世界',
    'outside_border': '越出世界边界而死', 'generic': '死亡', 'generic_kill': '被指令杀死', 'magic': '被魔法杀死',
    'indirect_magic': '被魔法杀死', 'wither': '凋零而死', 'dragon_breath': '被龙息杀死', 'dry_out': '脱水而死',
    'sweet_berry_bush': '被甜浆果丛扎死', 'freeze': '冻死', 'stalagmite': '被石笋刺死',
    'falling_block': '被掉落的方块砸死', 'falling_anvil': '被铁砧砸死', 'falling_stalactite': '被钟乳石砸死',
    'sting': '被蜇死', 'mob_attack': '被生物杀死', 'mob_attack_no_aggro': '被生物杀死', 'player_attack': '被玩家杀死',
    'arrow': '被射杀', 'trident': '被三叉戟杀死', 'mob_projectile': '被射杀', 'spit': '被羊驼口水杀死',
    'fireworks': '被烟花炸死', 'fireball': '被火球烧死', 'unattributed_fireball': '被火球烧死',
    'wither_skull': '被凋灵之首杀死', 'thrown': '被投掷物砸死', 'thorns': '被荆棘反伤致死', 'explosion': '被炸死',
    'player_explosion': '被炸死', 'sonic_boom': '被音波尖啸杀死', 'bad_respawn_point': '被重生点爆炸炸死',
    'wind_charge': '被风弹杀死', 'mace_smash': '被重锤砸死'}

# Bukkit DamageCause → phrase, used when no damage type key was captured.
DAMAGE_CAUSES = {
    'LAVA': '掉进岩浆烧死', 'FALL': '摔死', 'DROWNING': '淹死', 'FIRE': '被火烧死', 'FIRE_TICK': '被烧死',
    'ENTITY_ATTACK': '被攻击致死', 'ENTITY_SWEEP_ATTACK': '被横扫攻击致死', 'PROJECTILE': '被射杀',
    'BLOCK_EXPLOSION': '被炸死', 'ENTITY_EXPLOSION': '被炸死', 'VOID': '掉出世界', 'SUFFOCATION': '窒息而死',
    'STARVATION': '饿死', 'POISON': '中毒而死', 'MAGIC': '被魔法杀死', 'WITHER': '凋零而死',
    'FALLING_BLOCK': '被掉落的方块砸死', 'THORNS': '被荆棘反伤致死', 'DRAGON_BREATH': '被龙息杀死', 'CUSTOM': '死亡',
    'FLY_INTO_WALL': '撞墙而死', 'HOT_FLOOR': '被岩浆块烫死', 'CRAMMING': '被挤压致死', 'FREEZE': '冻死',
    'LIGHTNING': '被闪电劈死', 'CONTACT': '被扎死', 'SONIC_BOOM': '被音波尖啸杀死', 'SUICIDE': '自杀',
    'KILL': '被指令杀死', 'WORLD_BORDER': '越出世界边界而死', 'CAMPFIRE': '被营火烧死', 'MELTING': '融化',
    'DRYOUT': '脱水而死'}
RANGED = frozenset({'arrow', 'trident', 'mob_projectile', 'PROJECTILE'})
EXPLOSIVE = frozenset({'explosion', 'player_explosion', 'fireworks', 'BLOCK_EXPLOSION', 'ENTITY_EXPLOSION'})

ADVANCEMENTS = {
    'story/root': 'Minecraft', 'story/mine_stone': '石器时代', 'story/upgrade_tools': '获得升级',
    'story/smelt_iron': '来硬的', 'story/obtain_armor': '整装上阵', 'story/lava_bucket': '热腾腾的',
    'story/iron_tools': '这不是铁镐么', 'story/deflect_arrow': '不吃这套，谢谢', 'story/form_obsidian': '冰桶挑战',
    'story/mine_diamond': '钻石！', 'story/enter_the_nether': '勇往直下', 'story/shiny_gear': '用钻石包裹我',
    'story/enchant_item': '附魔师', 'story/cure_zombie_villager': '僵尸科医生', 'story/follow_ender_eye': '隔墙有眼',
    'story/enter_the_end': '结束了？', 'nether/root': '下界', 'nether/return_to_sender': '见鬼去吧',
    'nether/find_bastion': '那些年，我们的堡垒', 'nether/obtain_ancient_debris': '深藏不露', 'nether/fast_travel': '曲速泡',
    'nether/find_fortress': '阴森的要塞', 'nether/obtain_crying_obsidian': '谁在切洋葱？', 'nether/distract_piglin': '金光闪闪',
    'nether/ride_strider': '这船有腿', 'nether/uneasy_alliance': '和平共处', 'nether/loot_bastion': '战猪',
    'nether/use_lodestone': '天涯共此石', 'nether/netherite_armor': '残骸裹身', 'nether/get_wither_skull': '恐怖的骷髅',
    'nether/obtain_blaze_rod': '与火共舞', 'nether/charge_respawn_anchor': '锚定一生', 'nether/explore_nether': '热门景点',
    'nether/summon_wither': '凋零山庄', 'nether/brew_potion': '本地酿造厂', 'nether/create_beacon': '带信标回家',
    'nether/all_potions': '狂乱的鸡尾酒', 'nether/create_full_beacon': '信标工程师', 'nether/all_effects': '为什么会变成这样呢？',
    'end/root': '末地', 'end/kill_dragon': '解放末地', 'end/dragon_egg': '下一世代', 'end/enter_end_gateway': '远程折跃',
    'end/respawn_dragon': '结束了……再一次……', 'end/dragon_breath': '你需要来点薄荷糖', 'end/find_end_city': '在游戏尽头的城市',
    'end/elytra': '天空即为极限', 'end/levitate': '这上面的风景不错', 'adventure/root': '冒险',
    'adventure/voluntary_exile': '自愿流放', 'adventure/kill_a_mob': '怪物猎人', 'adventure/trade': '成交！',
    'adventure/sleep_in_bed': '甜蜜的梦', 'adventure/hero_of_the_village': '村庄英雄', 'adventure/kill_all_mobs': '资深怪物猎人',
    'adventure/shoot_arrow': '瞄准目标', 'adventure/sniper_duel': '狙击手的对决', 'adventure/totem_of_undying': '超越生死',
    'adventure/summon_iron_golem': '招募援兵', 'adventure/adventuring_time': '探索的时光', 'adventure/throw_trident': '抖个机灵',
    'adventure/very_very_frightening': '非常非常恐怖', 'adventure/two_birds_one_arrow': '一箭双雕', 'adventure/arbalistic': '劲弩手',
    'adventure/whos_the_pillager_now': '现在谁才是掠夺者？', 'adventure/ol_betsy': '扣下扳机', 'adventure/bullseye': '正中靶心',
    'adventure/fall_from_world_height': '上天入地', 'adventure/kill_mob_near_sculk_catalyst': '它蔓延了',
    'adventure/avoid_vibration': '潜行100级', 'adventure/trade_at_world_height': '星际商人', 'adventure/salvage_sherd': '可敬的残骸',
    'husbandry/root': '农牧业', 'husbandry/plant_seed': '开荒垦地', 'husbandry/breed_an_animal': '我从哪儿来？',
    'husbandry/tame_an_animal': '永恒的伙伴', 'husbandry/fishy_business': '腥味十足的生意', 'husbandry/bred_all_animals': '成双成对',
    'husbandry/balanced_diet': '均衡饮食', 'husbandry/obtain_netherite_hoe': '终极奉献', 'husbandry/tactical_fishing': '战术性钓鱼',
    'husbandry/axolotl_in_a_bucket': '最萌捕食者', 'husbandry/complete_catalogue': '猫咪图鉴',
    'husbandry/safely_harvest_honey': '举巢欢庆', 'husbandry/silk_touch_nest': '举巢搬迁',
    'husbandry/tadpole_in_a_bucket': '带上一桶蝌蚪', 'husbandry/obtain_sniffer_egg': '闻香识蛋'}


def unknown(code):
    return f'（{code}）'


def world_name(code):
    return WORLDS.get(code) or f'世界{unknown(code)}'


def entity_name(code):
    return ENTITIES.get(str(code).upper()) or unknown(code)


def local(at, zone):
    return instant(at).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))


def stamp(at, zone):
    value = local(at, zone)
    return f'{value:%m-%d} 周{WEEKDAYS[value.weekday()]} {value:%H:%M}'


def clock(at, zone):
    value = local(at, zone)
    return f'周{WEEKDAYS[value.weekday()]} {value:%H:%M}'


def span(first, last, zone):
    """Server-computed '周三 21:03' (or a range) from evidence times; the model never writes times."""
    start, end = local(first, zone), local(last, zone)
    if end.replace(second=0, microsecond=0) == start.replace(second=0, microsecond=0):
        return clock(first, zone)
    if end.date() == start.date():
        return f'{clock(first, zone)}–{end:%H:%M}'
    return f'{clock(first, zone)}–{clock(last, zone)}'


def who(event):
    name = event.get('player_name') or '未知玩家'
    return name + ('（假人）' if event.get('is_fake') is True else '')


def place(facts):
    if not facts.get('world'):
        return ''
    coords = [facts.get(k) for k in ('x', 'y', 'z')]
    where = world_name(facts['world'])
    if all(isinstance(c, (int, float)) for c in coords):
        where += ' ' + ','.join(str(int(c)) for c in coords)
    return f'（{where}）'


def death(event, facts):
    damage = str(facts.get('damage_type') or '').split(':')[-1]
    cause = str(facts.get('cause') or '')
    killer = facts.get('related_entity_type')
    if facts.get('related_name') and (killer == 'PLAYER' or (not killer and facts.get('related_uuid'))):
        fake = '（假人）' if facts.get('related_is_fake') is True else ''
        verb = '射杀' if damage in RANGED or cause in RANGED else '炸死' if damage in EXPLOSIVE or cause in EXPLOSIVE else '杀死'
        return f"被 {facts['related_name']}{fake} {verb}"
    if killer and killer != 'PLAYER':
        mob = entity_name(killer)
        if damage in RANGED or cause in RANGED:
            return f'被{mob}射杀'
        if damage in EXPLOSIVE or cause in EXPLOSIVE:
            return f'被{mob}炸死'
        if damage in {'fireball', 'unattributed_fireball'}:
            return f'被{mob}的火球烧死'
        return f'被{mob}杀死'
    if damage:
        return DAMAGE_TYPES.get(damage) or f"死亡{unknown(facts['damage_type'])}"
    if cause:
        return DAMAGE_CAUSES.get(cause) or f'死亡{unknown(cause)}'
    return '死亡（记录里没有死因）'


def advancement(code):
    key = str(code or '').split(':', 1)[-1]
    title = ADVANCEMENTS.get(key)
    return f'达成进度「{title}」' if title else f'达成进度{unknown(code)}'


def describe(event, zone):
    """One plain-Chinese line per event, e.g. `10-14 周三 21:03 Steve 被僵尸杀死（主世界 120,64,-30）`."""
    facts, kind = event.get('facts') or {}, event.get('type')
    head = stamp(event['occurred_at'], zone)
    name = who(event)
    if kind == 'CHAT':
        content = str(facts.get('content') or '')[:300]
        if facts.get('cancelled'):
            return f'{head} {name} 的发言被拦截，没有发出：{content}'
        return f'{head} {name} 说：{content}'
    if kind == 'JOIN':
        return f'{head} {name} 进服'
    if kind == 'QUIT':
        reason = facts.get('reason') or facts.get('cause')
        note = QUIT_REASONS.get(reason, unknown(reason) if reason else '')
        return f'{head} {name} 退出' + (f'（{note}）' if note and not note.startswith('（') else note)
    if kind == 'KICK':
        cause = facts.get('cause')
        text = KICK_CAUSES.get(cause) if cause in KICK_CAUSES else unknown(cause) if cause else ''
        message = str(facts.get('reason') or '')[:80]
        line = f'{head} {name} 被踢出服务器'
        if text:
            line += text if text.startswith('（') else f'（{text}）'
        return line + (f'，提示：{message}' if message else '')
    if kind == 'DEATH':
        return f'{head} {name} {death(event, facts)}{place(facts)}'
    if kind == 'ADVANCEMENT':
        return f"{head} {name} {advancement(facts.get('reason'))}"
    if kind == 'WORLD_CHANGE':
        origin = str(facts.get('reason') or '')
        origin = origin[5:] if origin.startswith('from:') else origin
        target = facts.get('world')
        if origin and target:
            return f'{head} {name} 从{world_name(origin)}来到{world_name(target)}'
        return f'{head} {name} 切换世界' + (f'到{world_name(target)}' if target else '')
    if kind == 'COMMAND':
        return f"{head} {name} 执行命令 {str(facts.get('reason') or '')[:120]}"
    if kind == 'PRESENCE':
        return f'{head} {name} 在线{place(facts)}'
    if kind == 'RECORDER_START':
        return f'{head} 事件记录器启动（开服或插件重载）'
    if kind == 'RECORDER_STOP':
        return f'{head} 事件记录器停止（关服或插件重载，之后一段时间可能没有记录）'
    if kind == 'SERVER_START':
        return f'{head} 服务器启动'
    if kind == 'SERVER_STOP':
        return f'{head} 服务器停止'
    subject = f'{name} ' if event.get('player_name') else ''
    return f'{head} {subject}触发事件{unknown(kind)}'


def player_key(uid, name):
    if uid:
        return str(uid).lower()
    return 'name:' + name if name else None


def roster(events):
    """Stable per-run aliases (p1, p2 …) in order of first appearance."""
    players, index = [], {}
    def add(uid, name):
        key = player_key(uid, name)
        if not key:
            return
        if key not in index:
            index[key] = len(players)
            players.append({'alias': f'p{len(players) + 1}', 'uuid': uid, 'name': name or uid})
        elif name and players[index[key]]['name'] == players[index[key]]['uuid']:
            players[index[key]]['name'] = name
    for event in events:
        facts = event.get('facts') or {}
        add(event.get('player_uuid'), event.get('player_name'))
        if facts.get('related_entity_type') == 'PLAYER' or facts.get('related_uuid'):
            add(facts.get('related_uuid'), facts.get('related_name'))
    return players


class Roster:
    def __init__(self, players):
        self.players = players or []
        self.by_key = {player_key(p.get('uuid'), p.get('name') if not p.get('uuid') else None): p for p in self.players}
        self.by_alias = {p['alias'].lower(): p for p in self.players}

    def aliases(self, event):
        facts = event.get('facts') or {}
        found = []
        for uid, name in [(event.get('player_uuid'), event.get('player_name')), (facts.get('related_uuid'), facts.get('related_name'))]:
            entry = self.by_key.get(player_key(uid, name))
            if entry and entry['alias'] not in found:
                found.append(entry['alias'])
        return found

    def resolve(self, value):
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise ValueError('player must be an alias such as p1 or an exact player name')
        entry = self.by_alias.get(value.strip().lower())
        if entry:
            return entry
        matches = [p for p in self.players if str(p.get('name') or '').lower() == value.strip().lower()]
        if len(matches) == 1:
            return matches[0]
        raise ValueError('unknown player alias or name; use an alias from roster')

    def names(self):
        return {str(p.get('name') or '').lower() for p in self.players} - {''}


def view(event, people, zone):
    """What the model sees: a ref, the Chinese line and player aliases. No UUID/boot/capture/hash fields."""
    item = {'ref': event['event_id'], 'line': event.get('line') or describe(event, zone)}
    aliases = people.aliases(event)
    if aliases:
        item['who'] = aliases
    return item


# ---- v2 submission contract -------------------------------------------------

CERTAINTY = {'fact', 'inference', 'speculation'}
OUTCOME = {'resolved': '已解决', 'ongoing': '仍在继续', 'unknown': '结果不明'}
CERTAINTY_LABELS = {'fact': '明确事实', 'inference': '有依据的推断', 'speculation': '不确定的推测'}
BANNED = ('展开了', '引发了热议', '引发热议', '氛围融洽', '氛围热烈', '其乐融融', '生态', '脉络', '智能体',
          '事件显示', '记录显示', '数据显示', '见证了', '书写了', '谱写', '篇章', '画卷', '交织', '缩影', '不禁')
HEDGES = ('可能', '似乎', '或许', '大概', '也许', '疑似', '好像')
ALLOWED_CAPS = {'TNT', 'PVP', 'PVE', 'AI', 'QQ', 'BOSS', 'MC', 'ID', 'OK', 'NPC'}
CODE_WORDS = ({'CHAT', 'JOIN', 'QUIT', 'KICK', 'DEATH', 'COMMAND', 'PRESENCE', 'ADVANCEMENT', 'WORLD_CHANGE',
               'RECORDER_START', 'RECORDER_STOP', 'SERVER_START', 'SERVER_STOP'}
              | set(QUIT_REASONS) | set(KICK_CAUSES) | set(ENTITIES) | set(DAMAGE_CAUSES)) - ALLOWED_CAPS
UUID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
EVENT_RE = re.compile(r'mc:\d+:[a-z_]+:\d+', re.I)
ALIAS_RE = re.compile(r'(?<![A-Za-z0-9_])[pP]\d{1,4}(?![A-Za-z0-9_])')
CAPS_RE = re.compile(r'(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{2,}(?![A-Za-z0-9_])')
NAMESPACED_RE = re.compile(r'(?<![A-Za-z0-9_])[a-z_]+:[a-z0-9_/.]+')
CERTAINTY_RE = re.compile(r'(?<![A-Za-z])(fact|inference|speculation)(?![A-Za-z])', re.I)


def lint(text, names):
    """Reasons a prose field is unreadable for players; empty when it passes."""
    problems = []
    def hit(regex, label, exempt=frozenset()):
        found = [m.group(0) for m in regex.finditer(text) if m.group(0).lower() not in exempt]
        if found:
            problems.append(f'{label} {found[0]}')
    hit(EVENT_RE, '含事件编号')
    hit(UUID_RE, '含 UUID')
    hit(ALIAS_RE, '含玩家代号（正文写玩家名）', names)
    codes = [m.group(0) for m in CAPS_RE.finditer(text)
             if m.group(0).lower() not in names and (m.group(0) in CODE_WORDS or '_' in m.group(0))]
    if codes:
        problems.append(f'含英文代码 {codes[0]}（写中文）')
    hit(NAMESPACED_RE, '含英文代码', names)
    hit(CERTAINTY_RE, '含 fact/inference 字样')
    banned = [word for word in BANNED if word in text]
    if banned:
        problems.append(f'含空话“{banned[0]}”（写具体发生了什么）')
    if sum(text.count(word) for word in HEDGES) >= 2:
        problems.append('堆了多个“可能/似乎”（原因不明就写“记录里看不出原因”）')
    return problems


class Checker:
    def __init__(self, names):
        self.names, self.issues = names, []

    def text(self, value, path, limit):
        if not isinstance(value, str) or not value.strip():
            self.issues.append(f'{path} 不能为空')
            return ''
        value = value.strip()
        if len(value) > limit:
            self.issues.append(f'{path} 有 {len(value)} 字，超过 {limit} 字')
        self.issues.extend(f'{path} {problem}' for problem in lint(value, self.names))
        return value

    def refs(self, value, path, required=True, limit=20):
        if value is None and not required:
            return []
        if (not isinstance(value, list) or not (1 if required else 0) <= len(value) <= limit
                or any(not isinstance(ref, str) or not 1 <= len(ref) <= 128 for ref in value)):
            self.issues.append(f'{path} 需要 {1 if required else 0}~{limit} 个读过的事件 ref')
            return []
        if len(set(value)) != len(value):
            self.issues.append(f'{path} 有重复的 ref')
        return list(dict.fromkeys(value))

    def items(self, value, path, low, high):
        if value is None:
            value = []
        if not isinstance(value, list):
            self.issues.append(f'{path} 必须是数组')
            return []
        if not low <= len(value) <= high:
            self.issues.append(f'{path} 需要 {low}~{high} 条，现在 {len(value)} 条')
        return [(f'{path}[{i}]', item) for i, item in enumerate(value[:high]) if self.object(item, f'{path}[{i}]')]

    def object(self, value, path):
        if not isinstance(value, dict):
            self.issues.append(f'{path} 必须是对象')
            return False
        return True

    def note(self, item, path):
        return {'text': self.text(item.get('text'), path + '.text', 60),
                'evidence_event_ids': self.refs(item.get('evidence_event_ids'), path + '.evidence_event_ids')}


def validate(data, people, event_count, final):
    """Structure, length and readability checks. Raises one ValueError listing what to fix."""
    if not isinstance(data, dict):
        raise ValueError('report must be an object')
    if 'observations' in data and 'stories' not in data:
        raise ValueError('v2 report required: headline, highlights, stories, server_notes')
    check = Checker(people.names())
    nonempty = final and event_count > 0
    headline = data.get('headline')
    report = {'version': REPORT_VERSION,
              'headline': check.text(headline, 'headline', 50) if final or headline is not None else ''}
    report['highlights'] = [check.note(item, path) for path, item in
                            check.items(data.get('highlights'), 'highlights', min(3, event_count) if nonempty else 0, 6)]
    stories = []
    for path, item in check.items(data.get('stories'), 'stories', 1 if nonempty else 0, 12):
        timeline = [check.note(entry, p) for p, entry in
                    check.items(item.get('timeline'), path + '.timeline', min(2, event_count) if event_count else 0, 8)]
        outcome = item.get('outcome') if isinstance(item.get('outcome'), dict) else {}
        if outcome.get('status') not in OUTCOME:
            check.issues.append(f'{path}.outcome.status 只能是 resolved / ongoing / unknown')
        if item.get('certainty') not in CERTAINTY:
            check.issues.append(f'{path}.certainty 只能是 fact / inference / speculation')
        people_refs = item.get('people') or []
        if not isinstance(people_refs, list) or len(people_refs) > 20:
            check.issues.append(f'{path}.people 最多 20 个玩家代号')
            people_refs = []
        resolved = []
        for alias in people_refs:
            entry = people.by_alias.get(str(alias).lower()) if isinstance(alias, str) else None
            if not entry:
                check.issues.append(f'{path}.people 里的 {alias} 不在本周名单（用事件 who 里的代号）')
            elif entry['alias'] not in {r['alias'] for r in resolved}:
                resolved.append({'alias': entry['alias'], 'name': entry['name']})
        stories.append({'title': check.text(item.get('title'), path + '.title', 20),
                        'gist': check.text(item.get('gist'), path + '.gist', 60),
                        'timeline': timeline, 'people': resolved,
                        'outcome': {'status': outcome.get('status'), 'text': check.text(outcome.get('text'), path + '.outcome.text', 40)},
                        'certainty': item.get('certainty'),
                        'evidence_event_ids': check.refs(item.get('evidence_event_ids'), path + '.evidence_event_ids', False, 30)})
    report['stories'] = stories
    report['server_notes'] = [check.note(item, path) for path, item in check.items(data.get('server_notes'), 'server_notes', 0, 6)]
    if len(references(report)) > 400:
        check.issues.append('证据 ref 总数超过 400，挑最能说明问题的')
    if check.issues:
        shown = '；'.join(check.issues[:6])
        more = f'（另有 {len(check.issues) - 6} 处）' if len(check.issues) > 6 else ''
        raise ValueError(f'报告未通过校验，请修改后重新提交：{shown}{more}')
    return report


def references(report):
    refs = []
    for item in report['highlights'] + report['server_notes']:
        refs.extend(item['evidence_event_ids'])
    for story in report['stories']:
        refs.extend(story['evidence_event_ids'])
        for entry in story['timeline']:
            refs.extend(entry['evidence_event_ids'])
    return list(dict.fromkeys(refs))


def timed(report, times, zone):
    """Attach server-computed times from evidence; timeline and notes are then in chronological order."""
    def stamp_item(item):
        moments = sorted(times[ref] for ref in item['evidence_event_ids'])
        return {**item, 'at': span(moments[0], moments[-1], zone),
                'from': iso(moments[0]), 'to': iso(moments[-1])}
    def key(item):
        return item['from']
    stories = []
    for story in report['stories']:
        timeline = sorted((stamp_item(entry) for entry in story['timeline']), key=key)
        union = list(dict.fromkeys([ref for entry in timeline for ref in entry['evidence_event_ids']] + story['evidence_event_ids']))
        stories.append({**story, 'timeline': timeline, 'evidence_event_ids': union})
    return {**report, 'highlights': [stamp_item(item) for item in report['highlights']], 'stories': stories,
            'server_notes': sorted((stamp_item(item) for item in report['server_notes']), key=key)}


def iso(value):
    return instant(value).isoformat(timespec='milliseconds') + 'Z'


def observations(report):
    """v1 observations derived from v2, so the existing site renders without changes."""
    result = []
    if report['headline'] or report['highlights']:
        lines = [report['headline']] + [f"· {item['text']}" for item in report['highlights']]
        refs = list(dict.fromkeys(ref for item in report['highlights'] for ref in item['evidence_event_ids']))
        if refs:
            weakest = 'fact' if all(s['certainty'] == 'fact' for s in report['stories']) else 'inference'
            result.append({'title': '本周概览', 'summary': '\n'.join(line for line in lines if line),
                           'certainty': weakest, 'evidence_event_ids': refs})
    for story in report['stories']:
        summary = [story['gist']] + [f"{entry['at']} {entry['text']}" for entry in story['timeline']]
        summary.append(f"结果：{story['outcome']['text']}（{OUTCOME[story['outcome']['status']]}）")
        result.append({'title': story['title'], 'summary': '\n'.join(summary), 'certainty': story['certainty'],
                       'evidence_event_ids': story['evidence_event_ids']})
    if report['server_notes']:
        result.append({'title': '服务器状况', 'summary': '\n'.join(f"{item['at']} {item['text']}" for item in report['server_notes']),
                       'certainty': 'fact',
                       'evidence_event_ids': list(dict.fromkeys(r for item in report['server_notes'] for r in item['evidence_event_ids']))})
    return result


def markdown(report, server_id, period_start, zone, event_count):
    parts = [f"# {report['headline']}", f'服务器 {server_id} · {period_start} 起的一周 · 时区 {zone}']
    if not event_count:
        parts.append('本周期可用记录中没有可供调查的服务器事件；这不证明服务器没有活动。')
        return '\n\n'.join(parts)
    if report['highlights']:
        parts.append('## 本周要点\n\n' + '\n'.join(f"- {item['text']}" for item in report['highlights']))
    for story in report['stories']:
        block = [f"## {story['title']}", story['gist'], '\n'.join(f"- {entry['at']} {entry['text']}" for entry in story['timeline'])]
        result = f"结果：{story['outcome']['text']}（{OUTCOME[story['outcome']['status']]}）"
        if story['certainty'] != 'fact':
            result += f"\n\n可信度：{CERTAINTY_LABELS[story['certainty']]}"
        block.append(result)
        parts.append('\n\n'.join(line for line in block if line))
    if report['server_notes']:
        parts.append('## 服务器状况\n\n' + '\n'.join(f"- {item['at']} {item['text']}" for item in report['server_notes']))
    return '\n\n'.join(parts)


def digest(report_value, observations_value):
    """Previous weekly report as a short lead for historian_previous; v1 runs fall back to observations."""
    if isinstance(report_value, dict) and report_value.get('version') == REPORT_VERSION:
        return {'version': REPORT_VERSION, 'headline': report_value.get('headline'),
                'highlights': [item.get('text') for item in report_value.get('highlights', [])],
                'story_titles': [story.get('title') for story in report_value.get('stories', [])]}
    return {'version': 1, 'observations': [{'title': o.get('title'), 'summary': str(o.get('summary') or '')[:600],
                                            'certainty': o.get('certainty')} for o in (observations_value or [])]}
