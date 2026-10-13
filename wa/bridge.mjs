// Мост WhatsApp -> копилот. ТОЛЬКО читает: вызовов отправки сообщений здесь нет.
//
// Один процесс обслуживает WhatsApp всех пользователей: у каждого своя сессия в <корень>/<uid>/.
// Запускается из main.py (wa.py). События — в stdout, по одной JSON-строке, в каждом есть uid:
//   {"uid":1,"type":"status","status":"open|close|logged_out|pair_timeout","code":..}
//   {"uid":1,"type":"pair_code","code":"ABCD1234"} | {"uid":1,"type":"pair_code","error":"..."}
//   {"uid":1,"type":"msg","jid":"7701...@s.whatsapp.net","id":"...","ts":1700000000,"out":false,"text":"...","audio":false}
//   {"uid":1,"type":"media","id":"...","mime":"audio/ogg","data":"<base64>"}
// Команды — в stdin, тоже JSON-строками:
//   {"type":"start","uid":1}                    — подключить сохранённую сессию
//   {"type":"pair_code","uid":1,"phone":"7701…"} — привязать по коду (запустит сессию, если нужно)
//   {"type":"download","uid":1,"id":"..."}      — аудио голосового (main.py просит только для белого списка)
//   {"type":"logout","uid":1}                   — отвязать устройство и удалить сессию
// Белый список проверяет main.py: сообщения не из списка там сразу отбрасываются.
import fs from 'node:fs';
import path from 'node:path';
import pino from 'pino';
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  fetchLatestWaWebVersion,
  jidNormalizedUser,
  normalizeMessageContent,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';

const ROOT = path.resolve(process.argv[2] || 'wa-session');
// Только ошибки и только в stderr: подробные логи Baileys могут содержать текст сообщений.
// WA_DEBUG_LOG=путь — временно писать подробный лог в файл (для отладки привязки; потом удалить файл).
const logger = process.env.WA_DEBUG_LOG
  ? pino({ level: 'debug' }, pino.destination(process.env.WA_DEBUG_LOG))
  : pino({ level: 'error' }, pino.destination(2));

const RECENT_AUDIO_MAX = 300;
const PAIR_QR_CYCLES = 2; // сколько окон привязки (~2,5 мин каждое) ждём без привязки, потом останавливаемся

function emit(event) {
  process.stdout.write(JSON.stringify(event) + '\n');
}

/** Текст сообщения или null, если это служебное (реакция, удаление и т.п.). */
function textOf(message) {
  const m = normalizeMessageContent(message); // раскрывает «исчезающие» и «одноразовые» обёртки
  if (!m) return null;
  const text = m.conversation || m.extendedTextMessage?.text;
  if (text) return text;
  for (const kind of ['imageMessage', 'videoMessage', 'documentMessage']) {
    if (m[kind]) return m[kind].caption ? `[медиа] ${m[kind].caption}` : '[медиа]';
  }
  if (m.audioMessage) return '[голосовое]';
  if (m.stickerMessage) return '[стикер]';
  return null;
}

class Session {
  constructor(uid) {
    this.uid = uid;
    this.dir = path.join(ROOT, String(uid));
    this.sock = null;
    this.state = null;
    this.stopped = false;
    this.reconnectAttempt = 0;
    this.awaitingPairing = false; // соединение открыто и ждёт привязки
    this.pendingPairPhone = null; // запрошен код привязки — выполним при первом QR нового соединения
    this.qrCycles = 0;            // сколько окон привязки прошло впустую
    this.recentAudio = new Map(); // недавние голосовые: скачиваем, только если main.py попросит
  }

  emit(event) {
    emit({ uid: this.uid, ...event });
  }

  registered() {
    return Boolean(this.state?.creds?.registered);
  }

  clear() {
    fs.rmSync(this.dir, { recursive: true, force: true });
  }

  /** WhatsApp адресует часть чатов по LID ("...@lid") вместо номера. Приводим к "номер@s.whatsapp.net". */
  async phoneJid(key) {
    for (const jid of [key.remoteJid, key.remoteJidAlt]) {
      if (jid?.endsWith('@s.whatsapp.net')) return jidNormalizedUser(jid);
    }
    if (key.remoteJid?.endsWith('@lid')) {
      try {
        const pn = await this.sock.signalRepository.lidMapping.getPNForLID(key.remoteJid);
        if (pn) return jidNormalizedUser(pn);
      } catch {
        // сопоставления нет — такой чат пропустим
      }
    }
    return null;
  }

  async requestPairCode(phone) {
    try {
      this.emit({ type: 'pair_code', code: await this.sock.requestPairingCode(phone) });
    } catch (err) {
      this.emit({ type: 'pair_code', error: String(err?.message || err) });
    }
  }

  pairCode(phone) {
    if (this.registered()) {
      this.emit({ type: 'pair_code', error: 'WhatsApp уже привязан' });
      return;
    }
    // Код живёт, пока открыто текущее соединение (~2,5 мин). Начинаем свежее, чтобы на ввод было
    // всё окно: код запросим при первом QR нового соединения.
    this.pendingPairPhone = phone;
    this.qrCycles = 0;
    if (this.stopped || !this.sock) this.start();
    else if (this.awaitingPairing) this.sock.end(undefined);
  }

  async start() {
    this.stopped = false;
    fs.mkdirSync(this.dir, { recursive: true });
    const { state, saveCreds } = await useMultiFileAuthState(this.dir);
    this.state = state;
    // Версию берём с web.whatsapp.com: список в репозитории Baileys отстаёт, а с устаревшей
    // версией телефон отказывает в привязке («проверьте соединение»).
    let { version, isLatest } = await fetchLatestWaWebVersion({});
    if (!isLatest) ({ version } = await fetchLatestBaileysVersion());

    const sock = makeWASocket({
      auth: state,
      version,
      logger,
      markOnlineOnConnect: false, // не показывать «в сети» — уведомления на телефоне приходят как обычно
      syncFullHistory: false,
      // Начальную синхронизацию не отключаем: без неё Baileys не получает соответствие LID -> номер,
      // и привязка срывается. Саму историю (messaging-history.set) мы не слушаем — она никуда не уходит.
    });
    this.sock = sock;

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
      if (sock !== this.sock) return; // событие старого соединения
      if (qr) {
        this.awaitingPairing = true;
        if (this.pendingPairPhone) {
          const phone = this.pendingPairPhone;
          this.pendingPairPhone = null;
          await this.requestPairCode(phone);
        }
        // QR не показываем: пользователи привязывают по коду (QR нельзя отсканировать тем же телефоном)
      }
      if (connection === 'open') {
        this.awaitingPairing = false;
        this.reconnectAttempt = 0;
        this.qrCycles = 0;
        this.emit({ type: 'status', status: 'open' });
      }
      if (connection === 'close') {
        this.awaitingPairing = false;
        if (this.stopped) return;
        const code = lastDisconnect?.error?.output?.statusCode;
        if (code === DisconnectReason.loggedOut) {
          this.stopped = true;
          this.clear();
          this.emit({ type: 'status', status: 'logged_out' });
          return;
        }
        this.emit({ type: 'status', status: 'close', code: code ?? null });
        if (!this.registered()) {
          // Окно привязки истекло или запрошен новый код. Без запроса — ждём не бесконечно.
          if (!this.pendingPairPhone && ++this.qrCycles >= PAIR_QR_CYCLES) {
            this.stopped = true;
            this.clear();
            this.emit({ type: 'status', status: 'pair_timeout' });
            return;
          }
          setTimeout(() => this.start(), 1000);
          return;
        }
        const delay = Math.min(60000, 1000 * 2 ** this.reconnectAttempt);
        this.reconnectAttempt += 1;
        setTimeout(() => this.start(), delay);
      }
    });

    sock.ev.on('messages.upsert', async ({ messages }) => {
      for (const msg of messages) {
        const text = textOf(msg.message);
        if (!text) continue;
        const jid = await this.phoneJid(msg.key); // группы, статусы и каналы сюда не попадают
        if (!jid) continue;
        const audio = Boolean(normalizeMessageContent(msg.message)?.audioMessage);
        if (audio) {
          this.recentAudio.set(msg.key.id, msg);
          if (this.recentAudio.size > RECENT_AUDIO_MAX) this.recentAudio.delete(this.recentAudio.keys().next().value);
        }
        this.emit({
          type: 'msg',
          jid,
          id: msg.key.id,
          ts: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
          out: Boolean(msg.key.fromMe),
          text,
          audio,
        });
      }
    });
  }

  async download(id) {
    const msg = this.recentAudio.get(id);
    this.recentAudio.delete(id);
    if (!msg) return;
    try {
      const data = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: this.sock.updateMediaMessage });
      const mime = normalizeMessageContent(msg.message).audioMessage.mimetype || 'audio/ogg';
      this.emit({ type: 'media', id, mime: mime.split(';')[0], data: data.toString('base64') });
    } catch (err) {
      logger.error({ err: String(err) }, 'audio download failed');
    }
  }

  async logout() {
    this.stopped = true;
    try {
      if (this.registered()) await this.sock?.logout(); // устройство исчезнет из «Связанных устройств»
    } catch {
      // уже отвязано
    }
    try {
      this.sock?.end(undefined);
    } catch {
      // уже закрыто
    }
    this.clear();
  }
}

const sessions = new Map();

function session(uid) {
  if (!sessions.has(uid)) sessions.set(uid, new Session(uid));
  return sessions.get(uid);
}

async function handle(cmd) {
  const uid = Number(cmd.uid);
  if (!uid) return;
  if (cmd.type === 'start') {
    const s = session(uid);
    if (!s.sock || s.stopped) await s.start();
  } else if (cmd.type === 'pair_code') {
    await session(uid).pairCode(String(cmd.phone).replace(/\D/g, ''));
  } else if (cmd.type === 'download') {
    await sessions.get(uid)?.download(cmd.id);
  } else if (cmd.type === 'logout') {
    await (sessions.get(uid) || new Session(uid)).logout();
    sessions.delete(uid);
  }
}

let stdinBuf = '';
process.stdin.on('data', (chunk) => {
  stdinBuf += chunk;
  let nl;
  while ((nl = stdinBuf.indexOf('\n')) >= 0) {
    const line = stdinBuf.slice(0, nl);
    stdinBuf = stdinBuf.slice(nl + 1);
    let cmd;
    try {
      cmd = JSON.parse(line);
    } catch {
      continue;
    }
    handle(cmd).catch((err) => logger.error({ err: String(err), cmd: cmd.type }, 'command failed'));
  }
});
process.stdin.on('end', () => process.exit(0)); // main.py закрылся — выходим и мы
process.stdout.on('error', () => process.exit(0));
