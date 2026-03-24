import os
import random
import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands


TOKEN = os.getenv("TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DB_PATH = "/app/data/economia.db"

MOEDA = "✦ Solários"

BONUS_ENTRADA = 1000
BONUS_DAILY = 30
BONUS_ATIVIDADE = 10
COOLDOWN_ATIVIDADE_MIN = 5

BONUS_DESAFIO = 60
BONUS_EVENTO = 150
MULTA_FALTA = 100
BONUS_GERAL = 1000

RELIC_CHANCES = [
    ("comum", 55),
    ("rara", 25),
    ("lendaria", 10),
    ("vazia", 10),
]

RELIC_REWARDS = {
    "comum": (40, 80),
    "rara": (100, 180),
    "lendaria": (250, 400),
    "vazia": (0, 0),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def remaining_text(last_iso: str, hours: int = 24) -> str:
    then = parse_iso(last_iso)
    if then is None:
        return "0h 0m"

    next_time = then + timedelta(hours=hours)
    remaining = next_time - utc_now()

    if remaining.total_seconds() <= 0:
        return "0h 0m"

    total_seconds = int(remaining.total_seconds())
    h, rest = divmod(total_seconds, 3600)
    m, _ = divmod(rest, 60)
    return f"{h}h {m}m"


def roll_relic() -> tuple[str, int]:
    names = [name for name, _ in RELIC_CHANCES]
    weights = [weight for _, weight in RELIC_CHANCES]
    rarity = random.choices(names, weights=weights, k=1)[0]

    min_reward, max_reward = RELIC_REWARDS[rarity]
    reward = random.randint(min_reward, max_reward) if max_reward > 0 else 0
    return rarity, reward


class StarkBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)
        self.db: aiosqlite.Connection | None = None

    async def setup_hook(self):
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.execute("PRAGMA journal_mode=WAL;")
        await self.create_tables()
        await self.tree.sync()

    async def create_tables(self):
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                last_daily TEXT,
                last_relic TEXT,
                last_activity TEXT
            )
            """
        )

        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                valor INTEGER NOT NULL,
                descricao TEXT,
                autor_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )

        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        # Migração simples caso a tabela antiga já exista
        for column_sql in [
            "ALTER TABLE users ADD COLUMN last_daily TEXT",
            "ALTER TABLE users ADD COLUMN last_relic TEXT",
            "ALTER TABLE users ADD COLUMN last_activity TEXT",
        ]:
            try:
                await self.db.execute(column_sql)
            except Exception:
                pass

        try:
            await self.db.execute("ALTER TABLE transactions ADD COLUMN autor_id INTEGER")
        except Exception:
            pass

        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()


bot = StarkBot()


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


async def ensure_user(user_id: int):
    async with bot.db.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        await bot.db.execute(
            """
            INSERT INTO users (user_id, balance, last_daily, last_relic, last_activity)
            VALUES (?, 0, NULL, NULL, NULL)
            """,
            (user_id,),
        )
        await bot.db.commit()


async def get_balance(user_id: int) -> int:
    await ensure_user(user_id)
    async with bot.db.execute(
        "SELECT balance FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else 0


async def get_user_field(user_id: int, field: str):
    await ensure_user(user_id)
    async with bot.db.execute(
        f"SELECT {field} FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def set_user_field(user_id: int, field: str, value: str | None):
    await ensure_user(user_id)
    await bot.db.execute(
        f"UPDATE users SET {field} = ? WHERE user_id = ?",
        (value, user_id),
    )
    await bot.db.commit()


async def add_balance(
    user_id: int,
    amount: int,
    tipo: str,
    descricao: str = "",
    autor_id: int | None = None,
):
    await ensure_user(user_id)

    await bot.db.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
        (amount, user_id),
    )

    await bot.db.execute(
        """
        INSERT INTO transactions (user_id, tipo, valor, descricao, autor_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, tipo, amount, descricao, autor_id, utc_now().isoformat()),
    )
    await bot.db.commit()


async def set_meta(key: str, value: str):
    await bot.db.execute(
        """
        INSERT INTO meta (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    await bot.db.commit()


async def get_meta(key: str) -> str | None:
    async with bot.db.execute(
        "SELECT value FROM meta WHERE key = ?",
        (key,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def get_history(user_id: int, limit: int = 10):
    await ensure_user(user_id)
    async with bot.db.execute(
        """
        SELECT tipo, valor, descricao, created_at
        FROM transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return rows


@bot.event
async def on_ready():
    print(f"Stark online como {bot.user}")


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return

    await ensure_user(member.id)
    balance = await get_balance(member.id)

    if balance == 0:
        await add_balance(
            member.id,
            BONUS_ENTRADA,
            "entrada",
            "Bônus de entrada no servidor",
            autor_id=None,
        )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await ensure_user(message.author.id)

    last_activity = await get_user_field(message.author.id, "last_activity")
    now = utc_now()

    can_reward = True
    if last_activity:
        then = parse_iso(last_activity)
        if then and now < then + timedelta(minutes=COOLDOWN_ATIVIDADE_MIN):
            can_reward = False

    if can_reward:
        await add_balance(
            message.author.id,
            BONUS_ATIVIDADE,
            "atividade",
            f"Atividade no chat (+{BONUS_ATIVIDADE} a cada {COOLDOWN_ATIVIDADE_MIN} min)",
            autor_id=None,
        )
        await set_user_field(message.author.id, "last_activity", now.isoformat())

    await bot.process_commands(message)


@bot.tree.command(name="saldo", description="Mostra seu saldo atual.")
async def saldo(interaction: discord.Interaction):
    balance = await get_balance(interaction.user.id)
    embed = discord.Embed(
        title="Saldo atual",
        description=f"Você possui **{balance} {MOEDA}**.",
        color=discord.Color.dark_teal(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="daily", description="Receba sua recompensa diária.")
async def daily(interaction: discord.Interaction):
    user_id = interaction.user.id
    last_daily = await get_user_field(user_id, "last_daily")

    if last_daily:
        then = parse_iso(last_daily)
        if then and utc_now() < then + timedelta(hours=24):
            await interaction.response.send_message(
                f"Seu daily já foi coletado. Tente novamente em **{remaining_text(last_daily)}**.",
                ephemeral=True,
            )
            return

    await add_balance(
        user_id,
        BONUS_DAILY,
        "daily",
        "Recompensa diária",
        autor_id=None,
    )
    await set_user_field(user_id, "last_daily", utc_now().isoformat())

    balance = await get_balance(user_id)
    embed = discord.Embed(
        title="Daily coletado",
        description=(
            f"Você recebeu **{BONUS_DAILY} {MOEDA}**.\n"
            f"Saldo atual: **{balance} {MOEDA}**."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="pagar", description="Envia Solários para outra pessoa.")
@app_commands.describe(usuario="Quem vai receber", valor="Quantidade de Solários")
async def pagar(
    interaction: discord.Interaction,
    usuario: discord.Member,
    valor: app_commands.Range[int, 1, 999999999],
):
    if usuario.bot:
        await interaction.response.send_message(
            "Não é possível transferir Solários para bots.",
            ephemeral=True,
        )
        return

    if usuario.id == interaction.user.id:
        await interaction.response.send_message(
            "Você não pode pagar a si mesma.",
            ephemeral=True,
        )
        return

    saldo_atual = await get_balance(interaction.user.id)
    if saldo_atual < valor:
        await interaction.response.send_message(
            f"Saldo insuficiente. Você possui **{saldo_atual} {MOEDA}**.",
            ephemeral=True,
        )
        return

    await add_balance(
        interaction.user.id,
        -valor,
        "pagamento_enviado",
        f"Enviado para {usuario.id}",
        autor_id=interaction.user.id,
    )
    await add_balance(
        usuario.id,
        valor,
        "pagamento_recebido",
        f"Recebido de {interaction.user.id}",
        autor_id=interaction.user.id,
    )

    novo_saldo = await get_balance(interaction.user.id)
    embed = discord.Embed(
        title="Transferência concluída",
        description=(
            f"Você enviou **{valor} {MOEDA}** para {usuario.mention}.\n"
            f"Seu novo saldo é **{novo_saldo} {MOEDA}**."
        ),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="historico", description="Mostra suas movimentações recentes.")
async def historico(interaction: discord.Interaction):
    rows = await get_history(interaction.user.id, limit=10)

    if not rows:
        await interaction.response.send_message(
            "Nenhuma movimentação encontrada.",
            ephemeral=True,
        )
        return

    lines = []
    for tipo, valor, descricao, created_at in rows:
        sinal = "+" if valor >= 0 else ""
        lines.append(
            f"**{tipo}** · {sinal}{valor} {MOEDA}\n{descricao or 'Sem descrição'}"
        )

    embed = discord.Embed(
        title="Histórico",
        description="\n\n".join(lines[:10]),
        color=discord.Color.dark_embed(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="reliquia", description="Abra sua relíquia diária.")
async def reliquia(interaction: discord.Interaction):
    user_id = interaction.user.id
    last_relic = await get_user_field(user_id, "last_relic")

    if last_relic:
        then = parse_iso(last_relic)
        if then and utc_now() < then + timedelta(hours=24):
            await interaction.response.send_message(
                f"Sua relíquia de hoje já foi aberta. Volte em **{remaining_text(last_relic)}**.",
                ephemeral=True,
            )
            return

    rarity, reward = roll_relic()
    await set_user_field(user_id, "last_relic", utc_now().isoformat())

    if reward > 0:
        await add_balance(
            user_id,
            reward,
            "reliquia",
            f"Relíquia {rarity}",
            autor_id=None,
        )
        saldo_atual = await get_balance(user_id)
        desc = (
            f"Classificação: **{rarity.title()}**\n"
            f"Você recebeu **{reward} {MOEDA}**.\n"
            f"Saldo atual: **{saldo_atual} {MOEDA}**."
        )
        color = discord.Color.purple() if rarity == "lendaria" else discord.Color.dark_gold()
    else:
        desc = (
            "Classificação: **Vazia**\n"
            "Desta vez não havia Solários dentro da relíquia."
        )
        color = discord.Color.dark_grey()

    embed = discord.Embed(
        title="Relíquia aberta",
        description=desc,
        color=color,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------- ADMIN ---------------- #

def admin_only():
    async def predicate(interaction: discord.Interaction):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "Você não tem permissão para usar este comando.",
                ephemeral=True,
            )
            return False
        return True
    return app_commands.check(predicate)


@bot.tree.command(name="addsolarios", description="Adiciona Solários para um usuário.")
@app_commands.describe(usuario="Quem vai receber", valor="Quantidade")
@admin_only()
async def addsolarios(
    interaction: discord.Interaction,
    usuario: discord.Member,
    valor: app_commands.Range[int, 1, 999999999],
):
    await add_balance(
        usuario.id,
        valor,
        "admin_add",
        "Adição manual de Solários",
        autor_id=interaction.user.id,
    )
    saldo_atual = await get_balance(usuario.id)

    await interaction.response.send_message(
        f"Operação concluída.\n{usuario.mention} recebeu **{valor} {MOEDA}**.\nSaldo atual: **{saldo_atual} {MOEDA}**.",
        ephemeral=True,
    )


@bot.tree.command(name="removersolarios", description="Remove Solários de um usuário.")
@app_commands.describe(usuario="Quem vai perder", valor="Quantidade")
@admin_only()
async def removersolarios(
    interaction: discord.Interaction,
    usuario: discord.Member,
    valor: app_commands.Range[int, 1, 999999999],
):
    saldo_atual = await get_balance(usuario.id)
    valor_final = min(valor, saldo_atual)

    if valor_final <= 0:
        await interaction.response.send_message(
            f"{usuario.mention} não possui saldo para remover.",
            ephemeral=True,
        )
        return

    await add_balance(
        usuario.id,
        -valor_final,
        "admin_remove",
        "Remoção manual de Solários",
        autor_id=interaction.user.id,
    )
    saldo_novo = await get_balance(usuario.id)

    await interaction.response.send_message(
        f"Operação concluída.\nForam removidos **{valor_final} {MOEDA}** de {usuario.mention}.\nSaldo atual: **{saldo_novo} {MOEDA}**.",
        ephemeral=True,
    )


@bot.tree.command(name="desafio", description="Recompensa por desafio.")
@app_commands.describe(usuario="Usuário recompensado")
@admin_only()
async def desafio(interaction: discord.Interaction, usuario: discord.Member):
    await add_balance(
        usuario.id,
        BONUS_DESAFIO,
        "desafio",
        "Recompensa por desafio",
        autor_id=interaction.user.id,
    )
    saldo = await get_balance(usuario.id)
    await interaction.response.send_message(
        f"{usuario.mention} recebeu **{BONUS_DESAFIO} {MOEDA}** por desafio.\nSaldo atual: **{saldo} {MOEDA}**.",
        ephemeral=True,
    )


@bot.tree.command(name="evento", description="Recompensa por evento.")
@app_commands.describe(usuario="Usuário recompensado")
@admin_only()
async def evento(interaction: discord.Interaction, usuario: discord.Member):
    await add_balance(
        usuario.id,
        BONUS_EVENTO,
        "evento",
        "Recompensa por evento",
        autor_id=interaction.user.id,
    )
    saldo = await get_balance(usuario.id)
    await interaction.response.send_message(
        f"{usuario.mention} recebeu **{BONUS_EVENTO} {MOEDA}** por evento.\nSaldo atual: **{saldo} {MOEDA}**.",
        ephemeral=True,
    )


@bot.tree.command(name="falta", description="Aplica multa por falta.")
@app_commands.describe(usuario="Usuário penalizado")
@admin_only()
async def falta(interaction: discord.Interaction, usuario: discord.Member):
    saldo_atual = await get_balance(usuario.id)
    valor_final = min(MULTA_FALTA, saldo_atual)

    if valor_final <= 0:
        await interaction.response.send_message(
            f"{usuario.mention} não possui saldo para multar.",
            ephemeral=True,
        )
        return

    await add_balance(
        usuario.id,
        -valor_final,
        "falta",
        "Multa por falta",
        autor_id=interaction.user.id,
    )
    saldo = await get_balance(usuario.id)
    await interaction.response.send_message(
        f"{usuario.mention} recebeu multa de **{valor_final} {MOEDA}**.\nSaldo atual: **{saldo} {MOEDA}**.",
        ephemeral=True,
    )


@bot.tree.command(name="bonusgeral", description="Concede bônus geral uma única vez.")
@admin_only()
async def bonusgeral(interaction: discord.Interaction):
    already_used = await get_meta("bonusgeral_usado")

    if already_used == "1":
        await interaction.response.send_message(
            "O bônus geral já foi aplicado anteriormente.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "Este comando só pode ser usado dentro de um servidor.",
            ephemeral=True,
        )
        return

    membros = [m for m in interaction.guild.members if not m.bot]
    count = 0

    for membro in membros:
        await ensure_user(membro.id)
        await add_balance(
            membro.id,
            BONUS_GERAL,
            "bonusgeral",
            "Bônus geral inicial do servidor",
            autor_id=interaction.user.id,
        )
        count += 1

    await set_meta("bonusgeral_usado", "1")

    await interaction.response.send_message(
        f"Operação concluída com sucesso.\n**{count} membros** receberam **{BONUS_GERAL} {MOEDA}**.",
        ephemeral=True,
    )


async def main():
    if not TOKEN:
        raise RuntimeError("A variável de ambiente TOKEN não foi configurada.")

    if OWNER_ID == 0:
        print("AVISO: OWNER_ID não configurado. Comandos de admin não funcionarão.")

    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
