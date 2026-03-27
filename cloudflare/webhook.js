/**
 * SiftBot Cloudflare Worker — Telegram webhook handler.
 *
 * Handles:
 *   - Inline button presses (rating callbacks)
 *   - Bot commands: /start, /topics, /pause, /resume, /help
 *   - Free text (for multi-step /topics flow)
 *
 * Secrets required (set with `wrangler secret put`):
 *   TELEGRAM_BOT_TOKEN
 *   SUPABASE_URL
 *   SUPABASE_KEY
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("SiftBot webhook OK", { status: 200 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("Bad JSON", { status: 400 });
    }

    try {
      await handleUpdate(update, env);
    } catch (err) {
      console.error("Unhandled error:", err);
    }

    // Always return 200 so Telegram doesn't retry
    return new Response("OK", { status: 200 });
  },
};

// ── Dispatcher ────────────────────────────────────────────────────────────────

async function handleUpdate(update, env) {
  if (update.callback_query) {
    await handleCallbackQuery(update.callback_query, env);
    return;
  }

  if (update.message) {
    await handleMessage(update.message, env);
    return;
  }
}

// ── Callback queries (rating buttons) ────────────────────────────────────────

async function handleCallbackQuery(cbq, env) {
  const { id, from, data, message } = cbq;

  // Callback data format: "rate|<paper_id>|<score>"
  // Using | as separator because paper_ids contain colons (e.g. "arxiv:2403.12345")
  const parts = data.split("|");
  if (parts.length !== 3 || parts[0] !== "rate") {
    await answerCallback(env, id, "Unknown action");
    return;
  }

  const paperId = parts[1];
  const score = parseInt(parts[2], 10);

  if (![1, 5, 10].includes(score)) {
    await answerCallback(env, id, "Invalid score");
    return;
  }

  const ok = await writeRating(env, from.id, paperId, score);
  const emoji = score === 10 ? "❤️" : score === 5 ? "👍" : "👎";
  await answerCallback(env, id, ok ? `${emoji} Saved!` : "Couldn't save rating — try again");

  // Edit the message to show rating was received (append a line below the text)
  if (message && ok) {
    const existingText = message.text || "";
    if (!existingText.includes("✅")) {
      try {
        await telegramPost(env, "editMessageText", {
          chat_id: message.chat.id,
          message_id: message.message_id,
          text: existingText + `\n\n✅ Rated ${emoji}`,
          parse_mode: "Markdown",
          disable_web_page_preview: true,
        });
      } catch {
        // Non-critical — ignore edit errors
      }
    }
  }
}

// ── Message handler ───────────────────────────────────────────────────────────

async function handleMessage(message, env) {
  const { chat, text, from } = message;
  if (!text) return;

  const chatId = chat.id;

  // Bot commands
  if (text.startsWith("/start")) {
    await handleStart(env, chatId, from);
    return;
  }
  if (text.startsWith("/topics")) {
    await handleTopics(env, chatId);
    return;
  }
  if (text.startsWith("/pause")) {
    await handlePause(env, chatId);
    return;
  }
  if (text.startsWith("/resume")) {
    await handleResume(env, chatId);
    return;
  }
  if (text.startsWith("/help")) {
    await handleHelp(env, chatId);
    return;
  }

  // Free text — check pending_action
  const user = await getUser(env, chatId);
  if (user && user.pending_action === "awaiting_topics") {
    await saveTopics(env, chatId, text.trim());
    return;
  }

  // Unknown message
  await sendMessage(env, chatId, "I didn't understand that. Try /help");
}

// ── Command handlers ──────────────────────────────────────────────────────────

async function handleStart(env, chatId, from) {
  const existing = await getUser(env, chatId);
  if (existing) {
    const profile = existing.keyword_profile || "(none set)";
    await sendMessage(
      env,
      chatId,
      `Welcome back! Your current interests:\n\n_${profile}_\n\nSend /topics to update them.`,
      "Markdown"
    );
    return;
  }

  // Create user
  await upsertUser(env, chatId, { pending_action: "awaiting_topics" });
  await sendMessage(
    env,
    chatId,
    `👋 *Welcome to SiftBot!*\n\nI'll send you a daily digest of new scientific papers filtered to your interests.\n\nWhat topics are you interested in? Send a comma-separated list, for example:\n\n_computational pathology, vision language models, weakly supervised segmentation_`,
    "Markdown"
  );
}

async function handleTopics(env, chatId) {
  const user = await getUser(env, chatId);
  if (!user) {
    await sendMessage(env, chatId, "Please /start first.");
    return;
  }

  await setUserField(env, chatId, { pending_action: "awaiting_topics" });
  const current = user.keyword_profile ? `\n\nCurrent profile:\n_${user.keyword_profile}_` : "";
  await sendMessage(
    env,
    chatId,
    `📝 Send me your research interests as a comma-separated list:${current}`,
    "Markdown"
  );
}

async function handlePause(env, chatId) {
  await setUserField(env, chatId, { active: false });
  await sendMessage(env, chatId, "⏸ Digest paused. Send /resume to restart.");
}

async function handleResume(env, chatId) {
  await setUserField(env, chatId, { active: true });
  await sendMessage(env, chatId, "▶️ Digest resumed! You'll get papers again tomorrow.");
}

async function handleHelp(env, chatId) {
  await sendMessage(
    env,
    chatId,
    `*SiftBot Help*\n\n/start — register and set your interests\n/topics — update your research interests\n/pause — stop receiving daily digests\n/resume — restart daily digests\n/help — show this message\n\nRate papers with 👎 👍 ❤️ — after 30 ratings I'll personalise your digest.`,
    "Markdown"
  );
}

async function saveTopics(env, chatId, topics) {
  await setUserField(env, chatId, {
    keyword_profile: topics,
    pending_action: null,
    active: true,
  });
  await sendMessage(
    env,
    chatId,
    `✅ Got it! I'll filter papers for:\n\n_${topics}_\n\nYour next digest will arrive tomorrow morning.`,
    "Markdown"
  );
}

// ── Supabase helpers ──────────────────────────────────────────────────────────

async function supabaseRequest(env, method, path, body) {
  const url = `${env.SUPABASE_URL}/rest/v1/${path}`;
  const res = await fetch(url, {
    method,
    headers: {
      "Content-Type": "application/json",
      apikey: env.SUPABASE_KEY,
      Authorization: `Bearer ${env.SUPABASE_KEY}`,
      Prefer: "return=representation",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Supabase ${method} ${path} → ${res.status}: ${text}`);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

async function getUser(env, chatId) {
  const data = await supabaseRequest(
    env,
    "GET",
    `users?chat_id=eq.${chatId}&limit=1`,
    null
  );
  return data && data.length > 0 ? data[0] : null;
}

async function upsertUser(env, chatId, extra = {}) {
  return supabaseRequest(env, "POST", "users?on_conflict=chat_id", {
    chat_id: chatId,
    ...extra,
  });
}

async function setUserField(env, chatId, fields) {
  return supabaseRequest(env, "PATCH", `users?chat_id=eq.${chatId}`, fields);
}

async function writeRating(env, userChatId, paperId, score) {
  try {
    await supabaseRequest(env, "POST", "ratings?on_conflict=user_chat_id,paper_id", {
      user_chat_id: userChatId,
      paper_id: paperId,
      score,
    });
    return true;
  } catch (err) {
    console.error("writeRating error:", err);
    return false;
  }
}

// ── Telegram API helpers ──────────────────────────────────────────────────────

async function telegramPost(env, method, body) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

async function sendMessage(env, chatId, text, parseMode) {
  const body = { chat_id: chatId, text };
  if (parseMode) body.parse_mode = parseMode;
  body.disable_web_page_preview = true;
  return telegramPost(env, "sendMessage", body);
}

async function answerCallback(env, callbackQueryId, text) {
  return telegramPost(env, "answerCallbackQuery", {
    callback_query_id: callbackQueryId,
    text,
  });
}
