// Заплатка для Baileys 7.0.0-rc14, запускается автоматически после `npm install` (postinstall).
//
// До завершения привязки authState.creds.me ещё пустой, а sendMessageAck читает creds.me.id —
// подтверждение уведомления падает с TypeError, телефон не получает ack и пишет
// «Не удалось привязать устройство, проверьте соединение». buildAckStanza meId и так
// считает необязательным, поэтому достаточно безопасного чтения creds.me?.id.
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);
const pkgDir = path.dirname(require.resolve('@whiskeysockets/baileys/package.json'));
const file = path.join(pkgDir, 'lib', 'Socket', 'messages-recv.js');

const from = 'buildAckStanza(node, errorCode, authState.creds.me.id)';
const to = 'buildAckStanza(node, errorCode, authState.creds.me?.id)';

const src = fs.readFileSync(file, 'utf8');
if (src.includes(to)) {
  console.log('patch-baileys: уже применена');
} else if (src.includes(from)) {
  fs.writeFileSync(file, src.replace(from, to));
  console.log('patch-baileys: применена');
} else {
  console.warn('patch-baileys: место не найдено — версия Baileys изменилась, проверьте, нужна ли заплатка');
}
