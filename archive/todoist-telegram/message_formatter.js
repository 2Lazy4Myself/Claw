// Formats fetched Todoist data into plain-text strings for Telegram.
// No markdown, no asterisks, no underscores - plain text only.

require("dotenv").config();

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function truncate(text, max = 60) {
  if (!text) return "";
  return text.length > max ? text.slice(0, max) + "..." : text;
}

function todayString() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function formatDDMon(dateStr) {
  const [, month, day] = dateStr.match(/^\d{4}-(\d{2})-(\d{2})$/);
  return `${parseInt(day, 10)} ${MONTHS[parseInt(month, 10) - 1]}`;
}

function formatTaskLine(task) {
  const title = truncate(task.content);
  const today = todayString();
  const isOverdue = task.due_date && task.due_date < today;

  if (isOverdue) {
    return `${title} 📅 ${formatDDMon(task.due_date)} ⚠️ OVERDUE`;
  }
  return title;
}

function formatWorkDigest(waitingTasks, unprocessedTasks) {
  const lines = [];

  lines.push("📋 Work Daily Digest");

  lines.push("");
  lines.push(`⏳ WAITING FOR (${waitingTasks.length}):`);
  if (waitingTasks.length === 0) {
    lines.push("Nothing here 👍");
  } else {
    for (const { task, waitingFor } of waitingTasks) {
      lines.push(`• ${truncate(task.content)} → ${waitingFor ?? "unknown"}`);
    }
  }

  lines.push("");
  lines.push(`📥 UNPROCESSED (${unprocessedTasks.length}):`);
  if (unprocessedTasks.length === 0) {
    lines.push("Nothing here 👍");
  } else {
    for (const task of unprocessedTasks) {
      lines.push(`• ${truncate(task.content)}`);
    }
  }

  return lines.join("\n");
}

function formatWorkNag(tasks, timeLabel, isEveningNag, unprocessedCount) {
  const lines = [];

  lines.push(`🔴 Work - ${timeLabel} Check In`);

  if (tasks.length === 0) {
    lines.push("");
    lines.push("Nothing outstanding - nice work 🎉");
    return lines.join("\n");
  }

  lines.push("");
  lines.push("Still to do:");
  for (const task of tasks) {
    lines.push(`• ${formatTaskLine(task)}`);
  }

  lines.push("");
  lines.push(`${tasks.length} items remaining`);

  if (isEveningNag) {
    lines.push("");
    lines.push(`📥 ${unprocessedCount} unprocessed items still waiting - clear them before tomorrow if you can`);
  }

  return lines.join("\n");
}

function formatHomeDigest(waitingTasks, unprocessedTasks) {
  const lines = [];

  lines.push("🏠 Home Daily Digest");

  lines.push("");
  lines.push(`⏳ WAITING FOR (${waitingTasks.length}):`);
  if (waitingTasks.length === 0) {
    lines.push("Nothing here 👍");
  } else {
    for (const { task, waitingFor } of waitingTasks) {
      lines.push(`• ${truncate(task.content)} → ${waitingFor ?? "unknown"}`);
    }
  }

  lines.push("");
  lines.push(`📥 UNPROCESSED (${unprocessedTasks.length}):`);
  if (unprocessedTasks.length === 0) {
    lines.push("Nothing here 👍");
  } else {
    for (const task of unprocessedTasks) {
      lines.push(`• ${truncate(task.content)}`);
    }
  }

  return lines.join("\n");
}

function formatHomeEveningNag(tasks) {
  const lines = [];

  lines.push("🏠 Home - Evening Check");

  lines.push("");
  if (tasks.length === 0) {
    lines.push("Nothing outstanding at home 👍");
  } else {
    lines.push("Still on your home list:");
    for (const task of tasks) {
      lines.push(`• ${formatTaskLine(task)}`);
    }
  }

  lines.push("");
  lines.push("Enjoy your evening 🌙");

  return lines.join("\n");
}

// --- Phase 2: MarkdownV2 weekly callout ---

const SECTION_ORDER = ['Next 2-3 Days', 'This Week', 'Next Week', 'This Month'];

function escapeMd(text) {
  return String(text).replace(/([_*\[\]()~`>#+\-=|{}.!?])/g, '\\$1');
}

function truncateMd(text, max = 55) {
  if (!text) return '';
  const t = text.length > max ? text.slice(0, max) + '…' : text;
  return escapeMd(t);
}

function taskRemark(sectionName, days, isSoftWarn) {
  switch (sectionName) {
    case 'Next 2-3 Days':
      return days >= 7 ? 'this was meant to be quick 😬' : 'still here\\? 👀';
    case 'This Week':
      return days >= 11
        ? "this has been 'this week' for over a week 😅"
        : 'been a week\\.\\.\\. 🤔';
    case 'Next Week':
      return days >= 18
        ? 'next week was *two* weeks ago mate 😬'
        : 'next week was last week 😅';
    case 'This Month':
      return isSoftWarn
        ? 'three weeks and counting\\.\\.\\. 👀'
        : 'a whole month\\. come on\\. 😬';
    default:
      return '';
  }
}

function formatWeeklyCallout(staleItems, project) {
  if (!staleItems || staleItems.length === 0) return null;

  const title = project === 'work'
    ? '📊 *Weekly Reality Check — Work*'
    : '📊 *Weekly Reality Check — Home*';

  const lines = [];
  lines.push(title);
  lines.push('');
  lines.push('These have been sitting there a while\\.\\.\\.');
  lines.push('');

  const bySection = {};
  for (const item of staleItems) {
    if (!bySection[item.section_name]) bySection[item.section_name] = [];
    bySection[item.section_name].push(item);
  }

  for (const section of SECTION_ORDER) {
    const tasks = bySection[section];
    if (!tasks || tasks.length === 0) continue;

    const maxDays = Math.max(...tasks.map(t => t.days_stale));
    const allSoftWarn = tasks.every(t => t.is_soft_warn);
    const escapedSection = escapeMd(section);
    const softSuffix = (section === 'This Month' && allSoftWarn) ? ' 👀' : '';
    lines.push(`*${escapedSection}* \\(${maxDays} days\\)${softSuffix}:`);

    for (const task of tasks.slice(0, 3)) {
      const remark = taskRemark(section, task.days_stale, task.is_soft_warn);
      lines.push(`• "${truncateMd(task.content)}" — ${remark}`);
    }
  }

  lines.push('');
  lines.push('_Either do them, move them, or kill them\\._');

  return lines.join('\n');
}

module.exports = {
  truncate,
  formatTaskLine,
  formatWorkDigest,
  formatWorkNag,
  formatHomeDigest,
  formatHomeEveningNag,
  formatWeeklyCallout,
};

if (require.main === module) {
  const {
    getTodayAndOverdueTasks,
    getWaitingForTasks,
    getUnprocessedTasks,
  } = require("./todoist_fetcher");

  const divider = () => console.log("─".repeat(40));

  (async () => {
    const [todayTasks, waitingTasks, unprocessedTasks] = await Promise.all([
      getTodayAndOverdueTasks("work"),
      getWaitingForTasks("work"),
      getUnprocessedTasks("work"),
    ]);

    divider();
    console.log(formatWorkDigest(waitingTasks, unprocessedTasks));

    divider();
    console.log(formatWorkNag(todayTasks, "12:00", false, unprocessedTasks.length));

    divider();
    console.log(formatWorkNag(todayTasks, "16:00", true, unprocessedTasks.length));

    divider();
    // Home formatters shown with work data as stand-in (Home fetch wired in scheduler)
    const homeTodayTasks = await getTodayAndOverdueTasks("home");
    const homeWaitingTasks = await getWaitingForTasks("home");
    const homeUnprocessedTasks = await getUnprocessedTasks("home");

    console.log(formatHomeDigest(homeWaitingTasks, homeUnprocessedTasks));

    divider();
    console.log(formatHomeEveningNag(homeTodayTasks));

    divider();
  })();
}
