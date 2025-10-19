#!/usr/bin/env python3
import asyncio
import re
import time
import os
import html
import threading
import json
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telebot import TeleBot, types

# ======== CONFIG ========

BOT_TOKEN = "8386282074:AAE8LhxPDch8cuaMNqTD2EKsuN7FaMUWO6w"
SESSION = "1BVtsOLIBu5Jhplupv_Ia-sB0XEpY2YDVuY5I-2wm2YiJ2CxBU00RPKSd8U2JzEQSWoNAZikFJ2ec7NSBvdCUBlWYt8MIPhxliWKXgkoXwkNU9ctm4gC52XpFzQxs8zvs0je178AliB790-4_NP4zjLZHJ7zbVEmTBxnwVR5RKzFylJDFTZfZJBIA7n-bzg_dcDaRmjXUJs0XCLqbpJn6JzuFmikA4eK5272t8TenWpk_11mqSowcMxZR81sSZiO4B3IojQG-KsWvroPCqtwoMUO4bVbPpWLTyWdtgM9-u9moqRTgAD7wD5E7eVUY1XpdQ2gm2z_ip45P1fcKjHHq5bWePWsi_xE="
API_ID = 22614616
API_HASH = "27ac13d47e2d34e03c006f9b6e821f1a"
MARNO_USERNAME = "@Mailstorm_emailbomber_bot"
OWNER_ID = 7853514708

PREMIUM_FILE = "premium.json"
GROUP_FILE = "group.json"
MAX_PER_REQUEST = 50
COOLDOWN_SECONDS = 60

# ======== GLOBAL VARS ========

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")
telethon_ready = threading.Event()
telethon_loop = None
client = None

premium_users = set()
group_ids = set()
pending_map = {}
pending_order = []
last_request_time = {}
bot_data = {}
lock = threading.Lock()  # untuk aman dari race condition

# ======== AMAN TOTAL LOADER ========

def _to_int_if_valid(s):
    """Konversi string ke int jika valid (termasuk negatif seperti -1002930149919)."""
    if s is None:
        return None
    s = str(s).strip().lstrip("\ufeff")
    if not s:
        return None
    if s[0] in "+-":
        if s[1:].isdigit():
            try:
                return int(s)
            except:
                return None
    elif s.isdigit():
        try:
            return int(s)
        except:
            return None
    return None


def safe_load_ids(filepath):
    """Aman total: mendukung JSON, CSV, newline, angka negatif, dan tidak pernah crash."""
    try:
        if not os.path.exists(filepath):
            return set()

        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()

        if not raw:
            return set()

        raw = raw.strip().lstrip("\ufeff")

        # 1️⃣ Coba JSON
        try:
            data = json.loads(raw)
            ids = set()
            if isinstance(data, list):
                for x in data:
                    val = _to_int_if_valid(x)
                    if val is not None:
                        ids.add(val)
                return ids
            elif isinstance(data, dict):
                for k in data.keys():
                    val = _to_int_if_valid(k)
                    if val is not None:
                        ids.add(val)
                return ids
        except Exception:
            pass

        # 2️⃣ Fallback ke CSV / newline
        parts = re.split(r"[,\n;]+", raw)
        ids = set()
        for p in parts:
            val = _to_int_if_valid(p)
            if val is not None:
                ids.add(val)
        return ids

    except Exception as e:
        print(f"⚠️ Gagal baca {filepath}: {e}")
        return set()

# ======== AUTO RELOAD PREMIUM & GROUP ========

def reload_files_loop():
    global premium_users, group_ids
    last_p = ""
    last_g = ""
    while True:
        try:
            p = ""
            g = ""
            if os.path.exists(PREMIUM_FILE):
                with open(PREMIUM_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    p = f.read()
            if os.path.exists(GROUP_FILE):
                with open(GROUP_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    g = f.read()

            if p != last_p:
                new_p = safe_load_ids(PREMIUM_FILE)
                with lock:
                    premium_users = new_p
                last_p = p
                print(f"✅ Premium list updated ({len(premium_users)} user)")

            if g != last_g:
                new_g = safe_load_ids(GROUP_FILE)
                with lock:
                    group_ids = new_g
                last_g = g
                print(f"✅ Group list updated ({len(group_ids)} group)")

        except Exception as e:
            print("⚠️ Error reload loop:", e)

        time.sleep(10)

threading.Thread(target=reload_files_loop, daemon=True).start()

# ======== TELETHON SETUP ========

async def send_to_marno(user_id, email, jumlah, chat_id, msg_id):
    """Kirim command /bomb ke user target"""
    try:
        msg = f"/bomb {email} {jumlah}"
        await client.send_message(MARNO_USERNAME.lstrip("@"), msg)

        key = f"{user_id}|{email}|{jumlah}|{int(time.time()*1000)}"
        with lock:
            pending_map[key] = {"chat_id": chat_id, "msg_id": msg_id, "user_id": user_id}
            pending_order.append(key)

        print(f"📤 Sent to {MARNO_USERNAME}: {email} ({jumlah})")

    except Exception as e:
        print("⚠️ Gagal kirim ke Marno:", e)
        try:
            bot.send_message(chat_id, f"❌ Gagal kirim ke {MARNO_USERNAME}: {e}")
        except Exception:
            pass


def register_telethon_handler():
    @client.on(events.NewMessage(from_users=MARNO_USERNAME.lstrip("@")))
    async def marno_reply(event):
        raw_text = event.raw_text or ""
        text_lower = raw_text.lower()
        print("📩 Reply dari Marno:", raw_text)

        try:
            # 1) AUTO CONFIRM (cek case-insensitive)
            if "type confirm" in text_lower:
                try:
                    await event.respond("confirm")
                    print("✅ Auto confirm sent")
                except Exception as e:
                    print("⚠️ Gagal auto confirm:", e)
                return

            # 2) Jika mengandung persis "💥 Total emails sent" -> ambil angka dan notify
            #    (Jika tidak ada string persis itu, handler akan mengabaikan pesan)
            if "💥 Total emails sent" in raw_text:
                m = re.search(r"💥\s*Total emails sent\s*[:\-]?\s*(\d+)", raw_text)
                if m:
                    total = m.group(1)
                else:
                    total = "?"

                with lock:
                    if pending_order:
                        key = pending_order.pop(0)
                        info = pending_map.pop(key, None)
                    else:
                        info = None

                if info:
                    chat_id = info.get("chat_id")
                    bot.send_message(chat_id, f"Attacking Success number of attacks:{total}")
                    print(f"✅ Notified user {info.get('user_id')} - total={total}")
                else:
                    print("⚠️ Tidak ada pending task untuk dikirim.")
                return

            # 3) Jika bukan confirm dan tidak mengandung string spesifik -> abaikan
            print("ℹ️ Pesan Marno tidak memicu action (bukan 'type confirm' atau '💥 Total emails sent').")

        except Exception as e:
            print("⚠️ Error handler:", e)


def telethon_thread(loop):
    global client
    asyncio.set_event_loop(loop)
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

    async def main():
        await client.start()
        me = await client.get_me()
        print(f"✅ Ubot aktif sebagai {me.username or me.first_name}")
        register_telethon_handler()
        telethon_ready.set()
        await client.run_until_disconnected()

    try:
        loop.run_until_complete(main())
    except Exception as e:
        print("❌ Telethon crash:", e)

# ======== COMMAND /start DENGAN EDIT PESAN DAN VIDEO ========

@bot.message_handler(commands=["start"])
def start(message):
    try:
        user = message.from_user
        user_name = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name or 'Pengguna')}</a>"
        user_id = user.id

        # Cek status
        if user_id == OWNER_ID:
            status = "👑 Owner"
        elif user_id in premium_users:
            status = "💎 Premium"
        else:
            status = "🆓 Free User"

        video_url = "https://files.catbox.moe/nrus99.mp4"

        caption = (
            f"<b>Welcome: {user_name}</b>\n"
            f"Status: {status}\n"
             "Note:\n"
             "<pre>Gunakan Bot Ini Sebijak Mungkin🚀</pre>\n\n"
             "Owner@PensiBre"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("👤 Menu User", callback_data="menu_user"),
            types.InlineKeyboardButton("👑 Menu Owner", callback_data="menu_owner"),
        )

        sent = bot.send_video(
            chat_id=message.chat.id,
            video=video_url,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup,
        )

        # simpan message_id agar bisa diedit nanti
        try:
            bot_data[message.chat.id] = sent.message_id
        except Exception:
            # fallback jika send_video tidak mengembalikan objek lengkap
            pass

    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ Gagal kirim video: {e}")

# ======== CALLBACK MENU ========

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    try:
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        user = call.from_user

        # ========== MENU USER ==========
        if call.data == "menu_user":
            new_caption = (
                "👤 <b>Menu User</b>\n\n"
                "📧 Fitur:\n"
                "<code>/Attack email@example.com 10</code>\n"
                "<pre>Spam Email</pre>"
                "<code>/cek token bot telegram</code>\n"
                "<pre>Cek Nama Dan Username Bot</pre>\n\n"
                "💎 Buy Akses Premium Agar Bebas Tanpa Batas"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Kembali", callback_data="back_home"))

            bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg_id,
                caption=new_caption,
                parse_mode="HTML",
                reply_markup=markup
            )

        # ========== MENU OWNER ==========
        elif call.data == "menu_owner":
            if user.id != OWNER_ID:
                return bot.answer_callback_query(call.id, "🚫 Kamu bukan owner!", show_alert=True)

            new_caption = (
                "👑 <b>Menu Owner</b>\n\n"
                "🧩 <code>/addpremium &lt;id&gt;</code>\n"
                "🗑 <code>/delpremium &lt;id&gt;</code>\n"
                "➕ <code>/addgroup &lt;id&gt;</code>\n"
                "➖ <code>/delgroup &lt;id&gt;</code>\n"
                "📋 <code>/list</code>"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Kembali", callback_data="back_home"))

            bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg_id,
                caption=new_caption,
                parse_mode="HTML",
                reply_markup=markup
            )

        # ========== MENU UTAMA ==========
        elif call.data == "back_home":
            name_link = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name or 'Pengguna')}</a>"
            if user.id == OWNER_ID:
                status = "👑 Owner"
            elif user.id in premium_users:
                status = "💎 Premium"
            else:
                status = "🆓 Free User"

            new_caption = (
                f"<b>Welcome: {name_link}</b>\n"
                f"Status: {status}\n"
                 "Note:\n"
                 "<pre>Gunakan Bot Ini Sebijak Mungkin🚀</pre>\n\n"
                 "Owner@PensiBre"
            )

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("👤 Menu User", callback_data="menu_user"),
                types.InlineKeyboardButton("👑 Menu Owner", callback_data="menu_owner"),
            )

            bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg_id,
                caption=new_caption,
                parse_mode="HTML",
                reply_markup=markup
            )

        else:
            bot.answer_callback_query(call.id, "✅ OK")

    except Exception as e:
        print("⚠️ Callback error:", e)
        try:
            bot.answer_callback_query(call.id, "❌ Terjadi error, coba lagi.", show_alert=True)
        except:
            pass

# ======== COMMAND /Attack ========

@bot.message_handler(commands=["Attack"])
def attack(message):
    try:
        args = message.text.split()
        if len(args) != 3:
            return bot.reply_to(message, "⚙️ Format salah!\nGunakan: /Attack email jumlah")

        email, jumlah = args[1].strip(), args[2].strip()
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            return bot.reply_to(message, "❌ Format email salah.")
        if not jumlah.isdigit() or int(jumlah) <= 0:
            return bot.reply_to(message, "❌ Jumlah harus angka positif.")

        user_id = message.from_user.id
        chat_id = message.chat.id
        jumlah = int(jumlah)

        with lock:
            is_premium = user_id in premium_users
            is_allowed = chat_id in group_ids
            last_time = last_request_time.get(user_id, 0)

        if not is_allowed:
            return bot.reply_to(message, "⚠️ Hanya bisa digunakan di grup terdaftar.")

        if not is_premium and jumlah > MAX_PER_REQUEST:
            return bot.reply_to(message, f"⚠️ Maksimal {MAX_PER_REQUEST} untuk pengguna gratis.")

        if not is_premium and time.time() - last_time < COOLDOWN_SECONDS:
            sisa = int(COOLDOWN_SECONDS - (time.time() - last_time))
            return bot.reply_to(message, f"⏳ Tunggu {sisa} detik lagi.")

        with lock:
            last_request_time[user_id] = time.time()

        if not telethon_ready.is_set():
            return bot.reply_to(message, "⚠️ Ubot belum siap, tunggu beberapa detik.")

        bot.reply_to(message, "🚀 Email Attack Process")
        # schedule coroutine pada loop telethon
        try:
            asyncio.run_coroutine_threadsafe(
                send_to_marno(user_id, email, jumlah, chat_id, message.message_id),
                telethon_loop,
            )
        except Exception as e:
            print("⚠️ Gagal schedule send_to_marno:", e)
            bot.reply_to(message, "❌ Gagal melakukan proses (internal).")

    except Exception as e:
        print("⚠️ Error command:", e)
        bot.reply_to(message, f"⚠️ Terjadi error: {e}")

# ======== COMMAND /cek <token> (hapus pesan user lalu tampilkan nama & username bot dari token) ========

@bot.message_handler(commands=["cek"])
def cek_token(message):
    try:
        args = message.text.split()
        if len(args) != 2:
            return bot.reply_to(message, "⚙️ Format: /cek <token_bot>")

        token = args[1].strip()

        # Hapus pesan yang berisi token agar tidak tersisa di chat
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            print(f"⚠️ Gagal hapus pesan /cek: {e}")

        # Cek token dengan membuat TeleBot sementara (tidak menyimpan)
        try:
            temp_bot = TeleBot(token)
            me = temp_bot.get_me()
            if not me:
                raise Exception("Tidak dapat mengambil informasi bot")

            nama = me.first_name or "❌ Tidak tersedia"
            uname = f"@{me.username}" if getattr(me, "username", None) else "❌ Tidak ada username"

            bot.send_message(
                message.chat.id,
                f"✅ Token valid!\n<b>Nama Bot:</b> {html.escape(nama)}\n<b>Username:</b> {html.escape(uname)}",
                parse_mode="HTML"
            )
        except Exception as e:
            print("⚠️ Error cek token:", e)
            bot.send_message(message.chat.id, "❌ Token tidak valid atau error saat mengecek (pastikan token benar).")

    except Exception as e:
        print("⚠️ Exception di /cek:", e)
        try:
            bot.send_message(message.chat.id, "❌ Terjadi error saat memproses /cek.")
        except:
            pass

# ======== UTIL: SAVE LIST ========

def save_list(filepath, data_set):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(sorted(list(data_set)), f, ensure_ascii=False, indent=2)
        print(f"💾 File {filepath} disimpan ({len(data_set)} item)")
    except Exception as e:
        print(f"⚠️ Gagal simpan {filepath}: {e}")

# ======== OWNER COMMANDS ========

@bot.message_handler(commands=["addpremium"])
def add_premium(message):
    if message.from_user.id != OWNER_ID:
        return bot.reply_to(message, "❌ Kamu bukan owner.")
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return bot.reply_to(message, "⚙️ Format: /addpremium <user_id>")
    user_id = int(args[1])
    with lock:
        premium_users.add(user_id)
        save_list(PREMIUM_FILE, premium_users)
    bot.reply_to(message, f"✅ Berhasil menambahkan {user_id} ke premium list.")


@bot.message_handler(commands=["delpremium"])
def del_premium(message):
    if message.from_user.id != OWNER_ID:
        return bot.reply_to(message, "❌ Kamu bukan owner.")
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return bot.reply_to(message, "⚙️ Format: /delpremium <user_id>")
    user_id = int(args[1])
    with lock:
        if user_id in premium_users:
            premium_users.remove(user_id)
            save_list(PREMIUM_FILE, premium_users)
            bot.reply_to(message, f"🗑️ {user_id} dihapus dari premium list.")
        else:
            bot.reply_to(message, f"⚠️ {user_id} tidak ada di premium list.")


@bot.message_handler(commands=["addgroup"])
def add_group(message):
    if message.from_user.id != OWNER_ID:
        return bot.reply_to(message, "❌ Kamu bukan owner.")
    args = message.text.split()
    if len(args) != 2 or not args[1].lstrip("-").isdigit():
        return bot.reply_to(message, "⚙️ Format: /addgroup <group_id>")
    group_id = int(args[1])
    with lock:
        group_ids.add(group_id)
        save_list(GROUP_FILE, group_ids)
    bot.reply_to(message, f"✅ Berhasil menambahkan grup {group_id}.")


@bot.message_handler(commands=["delgroup"])
def del_group(message):
    if message.from_user.id != OWNER_ID:
        return bot.reply_to(message, "❌ Kamu bukan owner.")
    args = message.text.split()
    if len(args) != 2 or not args[1].lstrip("-").isdigit():
        return bot.reply_to(message, "⚙️ Format: /delgroup <group_id>")
    group_id = int(args[1])
    with lock:
        if group_id in group_ids:
            group_ids.remove(group_id)
            save_list(GROUP_FILE, group_ids)
            bot.reply_to(message, f"🗑️ Grup {group_id} dihapus dari list.")
        else:
            bot.reply_to(message, f"⚠️ Grup {group_id} tidak ditemukan.")


@bot.message_handler(commands=["list"])
def list_data(message):
    if message.from_user.id != OWNER_ID:
        return bot.reply_to(message, "❌ Kamu bukan owner.")
    with lock:
        p_list = "\n".join(map(str, sorted(premium_users))) or "❌ Kosong"
        g_list = "\n".join(map(str, sorted(group_ids))) or "❌ Kosong"
    text = f"<b>📜 Daftar Premium:</b>\n{p_list}\n\n<b>🏠 Daftar Group:</b>\n{g_list}"
    bot.reply_to(message, text)

# ======== STARTUP ========

def start_telebot():
    """Jalankan TeleBot polling (di thread terpisah)."""
    print("🔁 Menjalankan TeleBot (polling)...")
    # gunakan infinity_polling yang sudah kamu pakai -- ini menunggu dan otomatis reconnect
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=10)
        except Exception as e:
            print("⚠️ TeleBot polling error:", e)
            time.sleep(3)


if __name__ == "__main__":
    # buat event loop baru untuk Telethon
