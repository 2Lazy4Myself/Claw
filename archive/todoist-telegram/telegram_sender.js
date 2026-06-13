// Sends plain-text messages to Telegram via the Bot API.
// Never throws - all errors are caught and logged internally.

require("dotenv").config();
const axios = require("axios");

function timestamp() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function sendMessage(text, extraParams = {}) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  const url = `https://api.telegram.org/bot${token}/sendMessage`;

  try {
    await axios.post(url, { chat_id: chatId, text, ...extraParams });
    console.log(`[${timestamp()}] Message sent OK (${text.length} chars)`);
    return true;
  } catch (err) {
    const msg = err.response ? JSON.stringify(err.response.data) : err.message;
    console.error(`[${timestamp()}] Send failed: ${msg}`);
    return false;
  }
}

async function sendWithRetry(text, maxRetries = 3, extraParams = {}) {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    const ok = await sendMessage(text, extraParams);
    if (ok) return true;
    if (attempt < maxRetries) {
      console.log(`[${timestamp()}] Retry ${attempt} of ${maxRetries}...`);
      await sleep(5000);
    }
  }
  return false;
}

module.exports = { sendMessage, sendWithRetry };

if (require.main === module) {
  (async () => {
    const testMsg = `✅ OpenClaw telegram_sender.js test OK - ${timestamp()}`;

    if (process.env.DRY_RUN === "true") {
      console.log(`DRY RUN - would send: ${testMsg}`);
      return;
    }

    const ok = await sendWithRetry(testMsg);
    console.log(ok ? "Self-test passed." : "Self-test FAILED - message not delivered.");
  })();
}
