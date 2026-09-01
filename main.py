import asyncio
import logging
import aiohttp
import asyncpg
from aiogram.exceptions import TelegramRetryAfter
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import LinkPreviewOptions

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
BOT_TOKEN = "8844887017:AAF9tMdhzZOj3CCO2vSJEKST9u0ZF-_WAng"
CHANNEL_ID = "@mrktparsing"
DB_DSN = "postgresql://bot_user:bot_password@localhost:5433/mrkt_db"

API_URL_SALING = 'https://api.mrkt.land/api/v1/gifts/saling'
API_URL_COLLECTIONS = 'https://api.mrkt.land/api/v1/gifts/collections'
API_URL_MODELS = 'https://api.mrkt.land/api/v1/gifts/models'

HEADERS = {
    'accept': '*/*',
    'content-type': 'application/json',
    'origin': 'https://www.mrkt.land',
    'cookie': 'access_token=04cd9f16-6872-4ad3-8c68-efa90933535d',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
}

PAYLOAD_SALING = {
    'count': 30,
    'cursor': '',
    'modelNames': [],
    'collectionNames': [],
    'symbolNames': [],
    'backdropNames': [],
    'ordering': 'None',
    'lowToHigh': False,
}

# ==========================================
# 🧠 IN-MEMORY CACHE FOR FLOOR PRICES
# ==========================================
model_floors_cache = {}
collection_floors_cache = {}


def sanitize_key(c, m):
    if not c or not m:
        return None
    clean_c = str(c).replace("'", "").replace("’", "").replace(" ", "").strip().lower()
    clean_m = str(m).replace("'", "").replace("’", "").replace(" ", "").strip().lower()
    return f"{clean_c}_{clean_m}"


def clean_coll_name(c):
    if not c: return None
    return str(c).replace("'", "").replace("’", "").replace(" ", "").strip().lower()


async def populate_cache_once():
    global model_floors_cache, collection_floors_cache
    new_models_cache = {}
    new_collections_cache = {}

    active_collection_names = []

    try:
        async with aiohttp.ClientSession() as session:
            # 1. Update Collections (GET)
            async with session.get(API_URL_COLLECTIONS, headers=HEADERS) as resp:
                if resp.status == 200:
                    coll_data = await resp.json()
                    for item in coll_data:
                        col_title = item.get('title')
                        col_name = item.get('name')
                        nano_tons = item.get('floorPriceNanoTons')

                        exact_name = col_title or col_name
                        if exact_name and exact_name not in active_collection_names:
                            active_collection_names.append(exact_name)

                        if nano_tons is not None:
                            price = int(nano_tons) / 1_000_000_000
                            if col_title: new_collections_cache[clean_coll_name(col_title)] = price
                            if col_name: new_collections_cache[clean_coll_name(col_name)] = price
                else:
                    logging.warning(f"Failed to fetch collections. Status: {resp.status}")

            # 2. Update Models (POST - ONE BY ONE)
            for col_name in active_collection_names:
                models_payload = {"collections": [col_name]}

                async with session.post(API_URL_MODELS, headers=HEADERS, json=models_payload) as resp:
                    if resp.status == 200:
                        models_data = await resp.json()
                        for item in models_data:
                            c_n = item.get('collectionName')
                            m_n = item.get('modelName')
                            nano_tons = item.get('floorPriceNanoTons')

                            if nano_tons is not None:
                                price = int(nano_tons) / 1_000_000_000
                                key = sanitize_key(c_n, m_n)
                                if key:
                                    new_models_cache[key] = price

                # 🛡️ ANTI-BAN: Wait 1 full second between requests so we don't spam the API
                await asyncio.sleep(1.0)

        # Apply the new data
        collection_floors_cache = new_collections_cache
        model_floors_cache = new_models_cache

        logging.info(f"📊 CACHE READY: Loaded {len(collection_floors_cache)} Collections and {len(model_floors_cache)} Models.")

    except Exception as e:
        logging.error(f"Failed to populate cache: {e}")


async def background_cache_updater():
    while True:
        # 🛡️ ANTI-BAN: Wait 5 minutes (300 seconds) before updating floor prices again
        await asyncio.sleep(300)
        await populate_cache_once()


# ==========================================
# 🐘 DATABASE LOGIC (WITH AUTO-CLEANUP)
# ==========================================
async def init_db():
    conn = await asyncpg.connect(DB_DSN)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS processed_gifts (
            id TEXT PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.close()
    logging.info("Database initialized.")


async def is_new_gift(pool, gift_id):
    async with pool.acquire() as conn:
        result = await conn.execute('''
            INSERT INTO processed_gifts (id) 
            VALUES ($1) 
            ON CONFLICT (id) DO NOTHING
        ''', str(gift_id))
        return result == "INSERT 0 1"


async def auto_clean_database(pool: asyncpg.Pool):
    while True:
        try:
            async with pool.acquire() as conn:
                result = await conn.execute("DELETE FROM processed_gifts WHERE added_at < NOW() - INTERVAL '3 days'")
                deleted_count = result.split()[-1]
                logging.info(f"🧹 DB Cleanup: Removed {deleted_count} old listings.")
        except Exception as e:
            logging.error(f"Database cleanup error: {e}")
        await asyncio.sleep(43200)


# ==========================================
# ✉️ TELEGRAM FORMATTING
# ==========================================
async def send_telegram_alert(bot: Bot, gift):
    raw_id = gift.get('id') or gift.get('name', '')
    clean_mrkt_id = str(raw_id).replace('-', '')

    collection = gift.get('collectionName', 'Unknown')
    number = gift.get('number', '0')
    model = gift.get('modelName', 'Unknown')
    backdrop = gift.get('backdropName', 'Unknown')
    symbol = gift.get('symbolName', 'Unknown')

    model_rarity = gift.get('modelRarityPerMille', 0) / 10
    backdrop_rarity = gift.get('backdropRarityPerMille', 0) / 10

    raw_sale_price = gift.get('salePrice')
    ton_price = int(raw_sale_price) / 1_000_000_000 if raw_sale_price else "Unknown"

    search_model_key = sanitize_key(collection, model)
    search_coll_key = clean_coll_name(collection)

    model_floor_val = model_floors_cache.get(search_model_key)
    model_floor = f"{model_floor_val:.2f}" if model_floor_val is not None else "Unknown"

    coll_floor_val = collection_floors_cache.get(search_coll_key)
    coll_floor = f"{coll_floor_val:.2f}" if coll_floor_val is not None else "Unknown"

    mrkt_url = f"https://t.me/mrkt/app?startapp={clean_mrkt_id}"
    clean_collection_hashtag = str(collection).replace(' ', '').replace("'", "").replace("’", "")
    clean_model_hashtag = str(model).replace(' ', '').replace("'", "").replace("’", "")
    tg_nft_url = f"https://t.me/nft/{clean_collection_hashtag}-{number}"

    text = f"🚨 <b>NEW LISTING DETECTED!</b>\n\n"
    text += f"<a href='{tg_nft_url}'><b>{collection} #{number}</b></a>\n"
    text += f"💰 <b>Price: {ton_price} TON</b>\n"
    text += f"📉 <i>Collec. Floor: {coll_floor} TON</i>\n\n"

    text += f"<b>Model:</b> {model} ({model_rarity}%)\n"
    text += f"↳ <i>Model Floor: {model_floor} TON</i>\n"
    text += f"<b>Backdrop:</b> {backdrop} ({backdrop_rarity}%)\n"
    text += f"<b>Symbol:</b> {symbol}\n\n"

    text += f"#{clean_collection_hashtag} #{clean_model_hashtag} #L\n\n"
    text += "PM @dancewithnightwind to list yours."

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="View on MRKT 👀", url=mrkt_url)]
    ])

    # 🛡️ FLOOD CONTROL PROTECTION LOOP
    max_retries = 5
    for attempt in range(max_retries):
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=False)
            )
            logging.info(f"✅ Sent alert for {collection} #{number}")
            break  # Success! Break out of the retry loop.

        except TelegramRetryAfter as e:
            # If Telegram says "Wait 10 seconds", we wait exactly 10 seconds and loop again.
            logging.warning(
                f"⏳ Telegram Rate Limit Hit! Sleeping for {e.retry_after} seconds before retrying {collection} #{number}...")
            await asyncio.sleep(e.retry_after)

        except Exception as e:
            logging.error(f"Failed to send Telegram message: {e}")
            break  # It failed for a different reason (e.g. bad token), stop retrying.

# ==========================================
# 🔄 MAIN SCRAPER LOOP
# ==========================================
async def check_new_listings(bot: Bot, pool: asyncpg.Pool):
    logging.info("Started watching for new listings...")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.post(API_URL_SALING, headers=HEADERS, json=PAYLOAD_SALING) as response:
                    if response.status == 200:
                        data = await response.json()
                        gifts = data.get('gifts', [])

                        # Reverse the list so the oldest listings send first, ending with the newest!
                        for gift in reversed(gifts):
                            gift_id = gift.get('id') or gift.get('name')
                            if not gift_id:
                                continue

                            if await is_new_gift(pool, gift_id):
                                await send_telegram_alert(bot, gift)
                                # 🚦 NATURAL DELAY: Wait 1.5 seconds between sending messages
                                # to avoid making Telegram angry.
                                await asyncio.sleep(1.5)
                    else:
                        logging.warning(f"API Error {response.status}")

            except Exception as e:
                logging.error(f"Scraper error: {e}")

            await asyncio.sleep(25)


# ==========================================
# 🚀 APP RUNNER
# ==========================================
async def main():
    logging.basicConfig(level=logging.INFO)

    await init_db()
    pool = await asyncpg.create_pool(DB_DSN)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    logging.info("Warming up Cache Data before starting scraper...")
    await populate_cache_once()

    cache_task = asyncio.create_task(background_cache_updater())
    db_cleaner_task = asyncio.create_task(auto_clean_database(pool))
    scraper_task = asyncio.create_task(check_new_listings(bot, pool))

    await asyncio.gather(cache_task, db_cleaner_task, scraper_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")