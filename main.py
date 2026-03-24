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
DAILY_REWARD = 30
ENTRADA_REWARD = 1000

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


def remaining_text(last_iso: str) -> str:
    then = parse_iso(last_iso)
    if then is None:
        return "0h 0m"

    next_time = then + timedelta(days=1)
    remaining = next_time - utc_now()

    if remaining.total_seconds() <= 0:
        return "0h 0m"

    total_seconds = int(remaining.total_seconds())
    hours, rest = divmod(total_seconds, 3600)
    minutes, _ = divmod(rest, 60)
    return f"{hours}h {minutes}m"


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
                last_relic TEXT
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
                created_at TEXT NOT NULL
            )
            """
        )
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
            "INSERT INTO users (user_id, balance, last_daily, last_relic) VALUES (?, 0, NULL, NULL)",
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


async def add_balance(user_id: int, amount: int, tipo: str, descricao: str = ""):
    await ensure_user(user_id)

    await bot.db.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
        (amount, user_id),
    )
    await bot.db.execute(
        """
        INSERT INTO transactions (user_id, tipo, valor, descricao, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, tipo, amount, descricao, utc_now().isoformat()),
    )
    await bot.db.commit()


async def get_last_daily(user_id: int):
    await ensure_user(user_id)

    async with bot.db.execute(
        "SELECT last_daily FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()

    return row[0] if row else None


async def set_last_daily(user_id: int, when_iso: str):
    await bot.db.execute(
        "UPDATE users SET last_daily = ? WHERE user_id = ?",
        (when_iso, user_id),
    )
    await bot.db.commit()


async def get_last_relic(user_id: int):
    await ensure_user(user_id)

    async with bot.db.execute(
        "SELECT last_relic FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()

    return row[0] if row else None


async def set_last_relic(user_id: int, when_iso: str):
    await bot.db.execute(
        "UPDATE users SET last_relic = ? WHERE user_id = ?",
        (when_iso, user_id),
    )
    await bot.db.commit()


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
            ENTRADA_REWARD,
            "entrada",
            "Bônus de entrada no servidor",
        )


@bot.tree.command(name="saldo", description="Mostra seu saldo atual.")
async def saldo(interaction: discord.Interaction):
    balance = await get_balance(interaction.user.id)

    embed = discord.Embed(
        title="Saldo",
        description=f"Você possui **{balance} {MOEDA}**.",
        color=discord.Color.dark_teal(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="daily", description="Receba sua recompensa diária.")
async def daily(interaction: discord.Interaction):
    user_id = interaction.user.id
    last_daily = await get_last_daily(user_id)

    if last_daily:
        then = parse_iso(last_daily)
        if then and utc_now() < then + timedelta(days=1):
            await interaction.response.send_message(
                f"Seu daily já foi coletado. Tente novamente em **{remaining_text(last_daily)}**.",
                ephemeral=True,
            )
            return

    await add_balance(user_id, DAILY_REWARD, "daily", "Recompensa diária")
    await set_last_daily(user_id, utc_now().isoformat())

    balance = await get_balance(user_id)
    embed = discord.Embed(
        title="Daily coletado",
        description=(
            f"Você recebeu **{DAILY_REWARD} {MOEDA}**.\n"
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
    )
    await add_balance(
        usuario.id,
        valor,
        "pagamento_recebido",
        f"Recebido de {interaction.user.id}",
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


@bot.tree.command(name="reliquia", description="Abra sua relíquia diária.")
async def reliquia(interaction: discord.Interaction):
    user_id = interaction.user.id
    last_relic = await get_last_relic(user_id)

    if last_relic:
        then = parse_iso(last_relic)
        if then and utc_now() < then + timedelta(days=1):
            await interaction.response.send_message(
                f"Sua relíquia de hoje já foi aberta. Volte em **{remaining_text(last_relic)}**.",
                ephemeral=True,
            )
            return

    rarity, reward = roll_relic()
    await set_last_relic(user_id, utc_now().isoformat())

    if reward > 0:
        await add_balance(user_id, reward, "reliquia", f"Relíquia {rarity}")
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


@bot.tree.command(name="addsolarios", description="Adiciona Solários a um usuário.")
@app_commands.describe(usuario="Quem vai receber", valor="Quantidade")
async def addsolarios(
    interaction: discord.Interaction,
    usuario: discord.Member,
    valor: app_commands.Range[int, 1, 999999999],
):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Você não tem permissão para usar este comando.",
            ephemeral=True,
        )
        return

    await add_balance(
        usuario.id,
        valor,
        "admin_add",
        "Adição manual de Solários",
    )
    saldo_atual = await get_balance(usuario.id)

    await interaction.response.send_message(
        f"{usuario.mention} recebeu **{valor} {MOEDA}**.\nSaldo atual: **{saldo_atual} {MOEDA}**.",
        ephemeral=True,
    )


@bot.tree.command(name="removersolarios", description="Remove Solários de um usuário.")
@app_commands.describe(usuario="Quem vai perder", valor="Quantidade")
async def removersolarios(
    interaction: discord.Interaction,
    usuario: discord.Member,
    valor: app_commands.Range[int, 1, 999999999],
):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message(
            "Você não tem permissão para usar este comando.",
            ephemeral=True,
        )
        return

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
    )
    saldo_novo = await get_balance(usuario.id)

    await interaction.response.send_message(
        f"Foram removidos **{valor_final} {MOEDA}** de {usuario.mention}.\nSaldo atual: **{saldo_novo} {MOEDA}**.",
        ephemeral=True,
    )


async def main():
    if not TOKEN:
        raise RuntimeError("A variável de ambiente TOKEN não foi configurada.")

    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
