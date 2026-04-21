import json
import os
from typing import Dict, List, Optional, Set

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from discord.errors import Forbidden

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
POLICY_DIR = os.getenv("ROLE_POLICY_DIR", "policies")
DEV_GUILD_ID_RAW = os.getenv("DEV_GUILD_ID")
DEFAULT_LOG_CHANNEL_ID_RAW = os.getenv("DEFAULT_LOG_CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in .env")

DEV_GUILD_ID = int(DEV_GUILD_ID_RAW) if DEV_GUILD_ID_RAW else None
DEFAULT_LOG_CHANNEL_ID = int(DEFAULT_LOG_CHANNEL_ID_RAW) if DEFAULT_LOG_CHANNEL_ID_RAW else None

os.makedirs(POLICY_DIR, exist_ok=True)


def policy_path_for_guild(guild_id: int) -> str:
    return os.path.join(POLICY_DIR, f"{guild_id}.json")


def load_raw_policy(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_policy(path: str) -> Dict[int, Set[int]]:
    """
    Expected JSON format:
    {
      "ALLOWED_GIVERS": {
        "123456789012345678": [111111111111111111, 222222222222222222],
        "987654321098765432": [333333333333333333]
      }
    }
    Where key = role that is allowed to give roles,
    value = list of target roles that this giver-role can assign.
    """
    raw = load_raw_policy(path)

    allowed_raw: Dict[str, List[int]] = raw.get("ALLOWED_GIVERS", {})
    policy: Dict[int, Set[int]] = {}
    for giver_role_id, target_role_ids in allowed_raw.items():
        policy[int(giver_role_id)] = {int(rid) for rid in target_role_ids}
    return policy


def save_policy(path: str, policy: Dict[int, Set[int]]) -> None:
    try:
        serializable = load_raw_policy(path)
    except FileNotFoundError:
        serializable = {}

    serializable["ALLOWED_GIVERS"] = {
            str(giver_id): sorted(list(target_ids))
            for giver_id, target_ids in policy.items()
        }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def load_role_creators(path: str) -> Set[int]:
    raw = load_raw_policy(path)
    creators_raw = raw.get("ALLOWED_ROLE_CREATORS", [])
    return {int(role_id) for role_id in creators_raw}


def save_role_creators(path: str, creator_role_ids: Set[int]) -> None:
    try:
        serializable = load_raw_policy(path)
    except FileNotFoundError:
        serializable = {}

    serializable["ALLOWED_ROLE_CREATORS"] = sorted(list(creator_role_ids))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def load_blocked_role_creators(path: str) -> Set[int]:
    raw = load_raw_policy(path)
    creators_raw = raw.get("BLOCKED_ROLE_CREATORS", [])
    return {int(role_id) for role_id in creators_raw}


def save_blocked_role_creators(path: str, blocked_role_ids: Set[int]) -> None:
    try:
        serializable = load_raw_policy(path)
    except FileNotFoundError:
        serializable = {}

    serializable["BLOCKED_ROLE_CREATORS"] = sorted(list(blocked_role_ids))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
SYNCED = False


def is_owner_or_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False


def get_log_channel_id(guild: discord.Guild) -> Optional[int]:
    path = policy_path_for_guild(guild.id)
    try:
        raw = load_raw_policy(path)
        value = raw.get("LOG_CHANNEL_ID", None)
        return int(value) if value is not None else DEFAULT_LOG_CHANNEL_ID
    except FileNotFoundError:
        return DEFAULT_LOG_CHANNEL_ID
    except Exception:
        return DEFAULT_LOG_CHANNEL_ID


def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    channel_id = get_log_channel_id(guild)
    if channel_id is None:
        return None
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


async def send_log(guild: discord.Guild, message: str) -> None:
    channel = get_log_channel(guild)
    if channel is None:
        return
    try:
        await channel.send(message)
    except Exception as e:
        print(f"Failed to send log message: {e}")


@bot.event
async def on_ready():
    global SYNCED
    if not SYNCED:
        if DEV_GUILD_ID is not None:
            guild = discord.Object(id=DEV_GUILD_ID)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        SYNCED = True
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    if before.channel is None and after.channel is not None:
        await send_log(
            member.guild,
            f"🔊 {member.mention} подключился в войс: **{after.channel.name}**",
        )

    if before.channel is not None and after.channel is None:
        found_disconnect_by_mod = False
        async for entry in member.guild.audit_logs(
            limit=5, action=discord.AuditLogAction.member_disconnect
        ):
            if entry.target and entry.target.id == member.id and entry.user:
                if entry.user.id != member.id:
                    await send_log(
                        member.guild,
                        f"⛔ {entry.user.mention} отключил из войса {member.mention}",
                    )
                    found_disconnect_by_mod = True
                break
        if not found_disconnect_by_mod:
            await send_log(member.guild, f"🔈 {member.mention} вышел из войса")

    if before.self_mute != after.self_mute:
        if after.self_mute:
            await send_log(member.guild, f"🎤 {member.mention} выключил микрофон")
        else:
            await send_log(member.guild, f"🎤 {member.mention} включил микрофон")

    if before.self_deaf != after.self_deaf:
        if after.self_deaf:
            await send_log(member.guild, f"🎧 {member.mention} выключил наушники")
        else:
            await send_log(member.guild, f"🎧 {member.mention} включил наушники")

    if before.mute != after.mute:
        if after.mute:
            await send_log(member.guild, f"🔇 {member.mention} серверно заглушен")
        else:
            await send_log(member.guild, f"🔊 {member.mention} серверный мут снят")

    if before.deaf != after.deaf:
        if after.deaf:
            await send_log(member.guild, f"🚫🎧 {member.mention} серверно оглушен")
        else:
            await send_log(member.guild, f"✅🎧 {member.mention} серверное оглушение снято")


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
        if entry.target and entry.target.id == user.id and entry.user:
            await send_log(guild, f"🔨 Бан: {user.mention} (модератор: {entry.user.mention})")
            return


@bot.event
async def on_member_remove(member: discord.Member):
    async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
        if entry.target and entry.target.id == member.id and entry.user:
            await send_log(
                member.guild,
                f"👢 Кик: {member.mention} (модератор: {entry.user.mention})",
            )
            return


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.roles != after.roles:
        await handle_role_update(before, after)

    before_timeout = getattr(before, "communication_disabled_until", None)
    if before_timeout is None:
        before_timeout = getattr(before, "timed_out_until", None)

    after_timeout = getattr(after, "communication_disabled_until", None)
    if after_timeout is None:
        after_timeout = getattr(after, "timed_out_until", None)
    if before_timeout == after_timeout or after_timeout is None:
        return

    if after_timeout > discord.utils.utcnow():
        async for entry in after.guild.audit_logs(
            limit=8, action=discord.AuditLogAction.member_update
        ):
            if entry.target and entry.target.id == after.id and entry.user:
                await send_log(
                    after.guild,
                    f"⏱️ Тайм-аут: {after.mention} до {after_timeout:%d.%m %H:%M} "
                    f"(модератор: {entry.user.mention})",
                )
                return


async def handle_role_update(before: discord.Member, after: discord.Member):
    if before.roles == after.roles:
        return

    added_roles = [r for r in after.roles if r not in before.roles]
    if not added_roles:
        return

    policy_file = policy_path_for_guild(before.guild.id)
    try:
        policy = load_policy(policy_file)
    except Exception as e:
        print(f"Failed to load policy file: {e}")
        return

    async for entry in before.guild.audit_logs(limit=8, action=discord.AuditLogAction.member_role_update):
        if entry.target.id != after.id:
            continue

        actor = entry.user
        if actor is None or actor.bot:
            return

        actor_member = before.guild.get_member(actor.id)
        if actor_member is None:
            return

        # Restrict only members that have at least one configured "giver" role.
        # If user has no governed giver roles, do not block their role updates.
        governed_giver_roles = [role for role in actor_member.roles if role.id in policy]
        if not governed_giver_roles:
            return

        # Accumulate all roles this actor is allowed to grant.
        allowed_targets: Set[int] = set()
        for giver_role in governed_giver_roles:
            allowed_targets.update(policy.get(giver_role.id, set()))

        blocked = [role for role in added_roles if role.id not in allowed_targets]
        if not blocked:
            return

        try:
            await after.remove_roles(*blocked, reason="Blocked by role-grant policy bot")
            blocked_names = ", ".join(role.name for role in blocked)
            await actor.send(
                f"Вы не можете выдавать эти роли: {blocked_names}. "
                "Выдача была автоматически отменена."
            )
        except Forbidden:
            blocked_names = ", ".join(role.name for role in blocked)
            print(
                "Missing permissions while removing blocked roles. "
                f"Could not remove: {blocked_names}. "
                "Ensure bot has Manage Roles and its role is above these target roles."
            )
        except Exception as e:
            print(f"Failed to remove blocked roles: {e}")
        return


@bot.event
async def on_guild_role_create(role: discord.Role):
    policy_file = policy_path_for_guild(role.guild.id)
    try:
        allowed_creator_roles = load_role_creators(policy_file)
        blocked_creator_roles = load_blocked_role_creators(policy_file)
    except Exception as e:
        print(f"Failed to load role creator rules: {e}")
        return

    if not allowed_creator_roles and not blocked_creator_roles:
        return

    async for entry in role.guild.audit_logs(limit=8, action=discord.AuditLogAction.role_create):
        if entry.target.id != role.id:
            continue

        actor = entry.user
        if actor is None or actor.bot:
            return

        actor_member = role.guild.get_member(actor.id)
        if actor_member is None:
            return

        # Server owner is always allowed.
        if actor_member.id == role.guild.owner_id:
            return

        actor_role_ids = {r.id for r in actor_member.roles}
        has_blocked_role = any(role_id in blocked_creator_roles for role_id in actor_role_ids)
        if has_blocked_role:
            pass
        elif not allowed_creator_roles:
            return
        elif any(role_id in allowed_creator_roles for role_id in actor_role_ids):
            return

        try:
            role_name = role.name
            await role.delete(reason="Blocked role creation by policy bot")
            await actor.send(
                f"У вас нет доступа на создание ролей. Роль `{role_name}` была удалена."
            )
        except Forbidden:
            print(
                "Missing permissions while deleting unauthorized created role. "
                "Ensure bot has Manage Roles and its role is above created role."
            )
        except Exception as e:
            print(f"Failed to delete unauthorized created role: {e}")
        return


@bot.tree.command(
    name="настройка_ролей",
    description="Разрешить одной роли выдавать другую роль",
)
@app_commands.rename(giver_role="какая_роль", target_role="какую_роль")
@app_commands.describe(
    giver_role="Какая роль может выдавать",
    target_role="Какую роль ей разрешено выдавать",
)
@app_commands.default_permissions(administrator=True, manage_roles=True)
async def setup_roles(
    interaction: discord.Interaction,
    giver_role: discord.Role,
    target_role: discord.Role,
):
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return
    if not is_owner_or_admin(interaction):
        await interaction.response.send_message(
            "Эту команду может использовать только владелец сервера или администратор.",
            ephemeral=True,
        )
        return

    policy_file = policy_path_for_guild(interaction.guild.id)
    try:
        policy = load_policy(policy_file)
    except FileNotFoundError:
        policy = {}
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось прочитать файл правил: {e}",
            ephemeral=True,
        )
        return

    policy.setdefault(giver_role.id, set()).add(target_role.id)
    try:
        save_policy(policy_file, policy)
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось сохранить файл правил: {e}",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Готово: роль {giver_role.mention} теперь может выдавать {target_role.mention}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="ресет_роли",
    description="Сбросить все правила выдачи для выбранной роли",
)
@app_commands.rename(giver_role="какая_роль")
@app_commands.describe(giver_role="Роль, для которой нужно очистить правила")
@app_commands.default_permissions(administrator=True, manage_roles=True)
async def reset_role_rules(interaction: discord.Interaction, giver_role: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return
    if not is_owner_or_admin(interaction):
        await interaction.response.send_message(
            "Эту команду может использовать только владелец сервера или администратор.",
            ephemeral=True,
        )
        return

    policy_file = policy_path_for_guild(interaction.guild.id)
    try:
        policy = load_policy(policy_file)
    except FileNotFoundError:
        policy = {}
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось прочитать файл правил: {e}",
            ephemeral=True,
        )
        return

    existed = giver_role.id in policy
    policy.pop(giver_role.id, None)
    try:
        save_policy(policy_file, policy)
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось сохранить файл правил: {e}",
            ephemeral=True,
        )
        return

    if existed:
        await interaction.response.send_message(
            f"Правила для роли {giver_role.mention} сброшены.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"Для роли {giver_role.mention} не было сохраненных правил.",
            ephemeral=True,
        )


@bot.tree.command(
    name="настройка_создания_ролей",
    description="Настроить кто может и кто не может создавать роли",
)
@app_commands.rename(
    can_create_role="кто_может",
    cannot_create_role="кто_не_может",
)
@app_commands.describe(
    can_create_role="Кто может создавать роли",
    cannot_create_role="Кто не может создавать роли",
)
@app_commands.default_permissions(administrator=True, manage_roles=True)
async def setup_role_creation_access(
    interaction: discord.Interaction,
    can_create_role: Optional[discord.Role] = None,
    cannot_create_role: Optional[discord.Role] = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return
    if not is_owner_or_admin(interaction):
        await interaction.response.send_message(
            "Эту команду может использовать только владелец сервера или администратор.",
            ephemeral=True,
        )
        return

    if can_create_role is None and cannot_create_role is None:
        await interaction.response.send_message(
            "Укажи хотя бы одну графу: кто может или кто не может.",
            ephemeral=True,
        )
        return

    policy_file = policy_path_for_guild(interaction.guild.id)
    try:
        creators_can = load_role_creators(policy_file)
        creators_cannot = load_blocked_role_creators(policy_file)
    except FileNotFoundError:
        creators_can = set()
        creators_cannot = set()
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось прочитать файл правил: {e}",
            ephemeral=True,
        )
        return

    try:
        if can_create_role is not None:
            creators_can.add(can_create_role.id)
            creators_cannot.discard(can_create_role.id)
        if cannot_create_role is not None:
            creators_cannot.add(cannot_create_role.id)
            creators_can.discard(cannot_create_role.id)

        save_role_creators(policy_file, creators_can)
        save_blocked_role_creators(policy_file, creators_cannot)
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось сохранить файл правил: {e}",
            ephemeral=True,
        )
        return

    changed_lines: List[str] = []
    if can_create_role is not None:
        changed_lines.append(f"Может создавать роли: {can_create_role.mention}")
    if cannot_create_role is not None:
        changed_lines.append(f"Не может создавать роли: {cannot_create_role.mention}")

    await interaction.response.send_message(
        "Готово:\n" + "\n".join(changed_lines),
        ephemeral=True,
    )


@bot.tree.command(
    name="ресет_создания_ролей",
    description="Убрать у роли доступ на создание новых ролей",
)
@app_commands.rename(creator_role="какая_роль")
@app_commands.describe(creator_role="Роль, у которой нужно убрать доступ")
@app_commands.default_permissions(administrator=True, manage_roles=True)
async def reset_role_creation_access(interaction: discord.Interaction, creator_role: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return
    if not is_owner_or_admin(interaction):
        await interaction.response.send_message(
            "Эту команду может использовать только владелец сервера или администратор.",
            ephemeral=True,
        )
        return

    policy_file = policy_path_for_guild(interaction.guild.id)
    try:
        creators_can = load_role_creators(policy_file)
        creators_cannot = load_blocked_role_creators(policy_file)
    except FileNotFoundError:
        creators_can = set()
        creators_cannot = set()
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось прочитать файл правил: {e}",
            ephemeral=True,
        )
        return

    existed = creator_role.id in creators_can or creator_role.id in creators_cannot
    creators_can.discard(creator_role.id)
    creators_cannot.discard(creator_role.id)
    try:
        save_role_creators(policy_file, creators_can)
        save_blocked_role_creators(policy_file, creators_cannot)
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось сохранить файл правил: {e}",
            ephemeral=True,
        )
        return

    if existed:
        await interaction.response.send_message(
            f"Доступ на создание ролей у {creator_role.mention} убран.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"У {creator_role.mention} и так не было доступа на создание ролей.",
            ephemeral=True,
        )


@bot.tree.command(
    name="правила_ролей",
    description="Показать текущие правила выдачи ролей",
)
@app_commands.default_permissions(administrator=True, manage_roles=True)
async def show_rules(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return

    policy_file = policy_path_for_guild(interaction.guild.id)
    try:
        policy = load_policy(policy_file)
        creators_can = load_role_creators(policy_file)
        creators_cannot = load_blocked_role_creators(policy_file)
    except Exception as e:
        await interaction.response.send_message(
            f"Не удалось прочитать файл правил: {e}",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    role_grant_lines: List[str] = []
    for giver_id, target_ids in policy.items():
        giver = guild.get_role(giver_id) if guild else None
        if giver is None:
            continue
        targets_text = []
        for target_id in sorted(target_ids):
            target = guild.get_role(target_id) if guild else None
            if target is not None:
                targets_text.append(target.mention)
        if targets_text:
            role_grant_lines.append(f"{giver.mention} -> {', '.join(targets_text)}")

    can_create_mentions: List[str] = []
    for role_id in sorted(creators_can):
        role = guild.get_role(role_id) if guild else None
        if role is not None:
            can_create_mentions.append(role.mention)

    cannot_create_mentions: List[str] = []
    for role_id in sorted(creators_cannot):
        role = guild.get_role(role_id) if guild else None
        if role is not None:
            cannot_create_mentions.append(role.mention)

    if not role_grant_lines and not can_create_mentions and not cannot_create_mentions:
        await interaction.response.send_message(
            "В списке нет актуальных правил для текущего сервера.",
            ephemeral=True,
        )
        return

    message_parts: List[str] = []
    if role_grant_lines:
        message_parts.append("Выдача ролей:\n" + "\n".join(role_grant_lines))
    if can_create_mentions:
        message_parts.append("Создание ролей (кто может):\n" + ", ".join(can_create_mentions))
    if cannot_create_mentions:
        message_parts.append("Создание ролей (кто не может):\n" + ", ".join(cannot_create_mentions))

    await interaction.response.send_message(
        "Текущие правила:\n\n" + "\n\n".join(message_parts),
        ephemeral=True,
    )


bot.run(TOKEN)
