// Main orchestration: wires fetcher, formatter, and sender together on a cron schedule.
// Run a specific job immediately: node scheduler.js workDigest

require("dotenv").config();
const cron = require("node-cron");
const fs = require("fs");
const path = require("path");

const { getTodayAndOverdueTasks, getWaitingForTasks, getUnprocessedTasks, getTasksForProject } = require("./todoist_fetcher");
const { formatWorkDigest, formatWorkNag, formatHomeDigest, formatHomeEveningNag, formatWeeklyCallout } = require("./message_formatter");
const { sendWithRetry } = require("./telegram_sender");
const config = require("./config");

const STATE_FILE = path.join(__dirname, "state.json");
const DRY_RUN = process.env.DRY_RUN === "true";

function ts() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function deliver(message, extraParams = {}) {
  if (DRY_RUN) {
    console.log(`[${ts()}] [DRY RUN]\n${message}`);
    return true;
  }
  return sendWithRetry(message, 3, extraParams);
}

// --- State ---

function readState() {
  try {
    const raw = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    if (raw.tasks && typeof raw.tasks === "object") {
      return raw;
    }
    return { tasks: raw };
  } catch {
    return { tasks: {} };
  }
}

function writeState(state) {
  const out = {
    _meta: {
      schema_version: 2,
      last_updated: ts(),
      phase2_deployed: true,
    },
    tasks: state.tasks,
  };
  fs.writeFileSync(STATE_FILE, JSON.stringify(out, null, 2));
}

function migrateState() {
  try {
    const state = readState();
    let dirty = false;
    for (const record of Object.values(state.tasks)) {
      if (record.first_seen && !record.section_entry_time) {
        record.section_entry_time = record.first_seen;
        dirty = true;
      }
    }
    if (dirty || !state._meta) {
      writeState(state);
      console.log(`[${ts()}] migrateState: state.json upgraded to schema v2`);
    }
  } catch (err) {
    console.error(`[${ts()}] migrateState error: ${err.message}`);
  }
}

function saveState(projectKey, tasks) {
  try {
    const state = readState();
    const now = ts();
    for (const task of tasks) {
      const existing = state.tasks[task.id];
      const sectionChanged = existing && existing.section_id !== task.section_id;
      state.tasks[task.id] = {
        id:                 task.id,
        content:            task.content,
        section_id:         task.section_id,
        section_name:       task.section_name,
        due_date:           task.due_date,
        first_seen:         existing ? existing.first_seen : now,
        section_entry_time: (!existing || sectionChanged) ? now : existing.section_entry_time,
        last_seen:          now,
        project:            projectKey,
      };
    }
    writeState(state);
  } catch (err) {
    console.error(`[${ts()}] saveState error: ${err.message}`);
  }
}

// --- Phase 2: Staleness ---

const STALENESS_SECTION_ORDER = ['Next 2-3 Days', 'This Week', 'Next Week', 'This Month'];

async function updateStalenessState(project) {
  try {
    const tasks = await getTasksForProject(project);
    const monitoredIds = new Set(Object.values(config.STALENESS_CONFIG.monitoredSections[project]));
    const monitored = tasks.filter(t => monitoredIds.has(t.section_id));
    saveState(project, monitored);
    console.log(`[${ts()}] updateStalenessState ${project}: ${monitored.length} monitored task(s) saved`);
  } catch (err) {
    console.error(`[${ts()}] updateStalenessState error: ${err.message}`);
  }
}

function getStaleItems(project) {
  const state = readState();
  const today = new Date().toISOString().slice(0, 10);
  const monitoredSections = config.STALENESS_CONFIG.monitoredSections[project];
  const monitoredIds = new Set(Object.values(monitoredSections));
  const sectionRank = Object.fromEntries(STALENESS_SECTION_ORDER.map((s, i) => [s, i]));

  const results = [];
  for (const task of Object.values(state.tasks)) {
    if (task.project !== project) continue;
    if (!monitoredIds.has(task.section_id)) continue;
    if (task.due_date && task.due_date >= today) continue;

    const entryTime = task.section_entry_time || task.first_seen;
    const days_stale = Math.floor((Date.now() - new Date(entryTime)) / 86400000);
    const { section_name } = task;
    const hardThreshold = config.STALENESS_CONFIG.thresholds[section_name];
    const softThreshold = config.STALENESS_CONFIG.softWarnThreshold[section_name];

    if (hardThreshold === undefined) continue;

    let is_soft_warn;
    if (days_stale >= hardThreshold) {
      is_soft_warn = false;
    } else if (softThreshold !== undefined && days_stale >= softThreshold) {
      is_soft_warn = true;
    } else {
      continue;
    }

    results.push({ content: task.content, section_name, days_stale, is_soft_warn, due_date: task.due_date ?? null });
  }

  results.sort((a, b) => {
    const diff = (sectionRank[a.section_name] ?? 99) - (sectionRank[b.section_name] ?? 99);
    return diff !== 0 ? diff : b.days_stale - a.days_stale;
  });

  return results;
}

// --- Jobs ---

async function workDigest() {
  console.log(`[${ts()}] workDigest starting`);
  const [waitingTasks, unprocessedTasks] = await Promise.all([
    getWaitingForTasks("work"),
    getUnprocessedTasks("work"),
  ]);
  const ok = await deliver(formatWorkDigest(waitingTasks, unprocessedTasks));
  console.log(`[${ts()}] workDigest done — waiting: ${waitingTasks.length}, unprocessed: ${unprocessedTasks.length}, delivered: ${ok}`);
  saveState("work", [...waitingTasks.map(w => w.task), ...unprocessedTasks]);
  await updateStalenessState("work");
}

async function workNag(timeLabel, isEveningNag = false) {
  console.log(`[${ts()}] workNag(${timeLabel}) starting`);
  const [tasks, unprocessedTasks] = await Promise.all([
    getTodayAndOverdueTasks("work"),
    isEveningNag ? getUnprocessedTasks("work") : Promise.resolve([]),
  ]);
  const ok = await deliver(formatWorkNag(tasks, timeLabel, isEveningNag, unprocessedTasks.length));
  console.log(`[${ts()}] workNag(${timeLabel}) done — tasks: ${tasks.length}, delivered: ${ok}`);
  saveState("work", tasks);
}

async function homeDigest() {
  console.log(`[${ts()}] homeDigest starting`);
  const [waitingTasks, unprocessedTasks] = await Promise.all([
    getWaitingForTasks("home"),
    getUnprocessedTasks("home"),
  ]);
  const ok = await deliver(formatHomeDigest(waitingTasks, unprocessedTasks));
  console.log(`[${ts()}] homeDigest done — waiting: ${waitingTasks.length}, unprocessed: ${unprocessedTasks.length}, delivered: ${ok}`);
  saveState("home", [...waitingTasks.map(w => w.task), ...unprocessedTasks]);
  await updateStalenessState("home");
}

async function homeNag() {
  // Morning home check - same format as evening, runs at 07:15 after homeDigest
  console.log(`[${ts()}] homeNag starting`);
  const tasks = await getTodayAndOverdueTasks("home");
  const ok = await deliver(formatHomeEveningNag(tasks));
  console.log(`[${ts()}] homeNag done — tasks: ${tasks.length}, delivered: ${ok}`);
  saveState("home", tasks);
}

async function homeEveningNag() {
  console.log(`[${ts()}] homeEveningNag starting`);
  const tasks = await getTodayAndOverdueTasks("home");
  const ok = await deliver(formatHomeEveningNag(tasks));
  console.log(`[${ts()}] homeEveningNag done — tasks: ${tasks.length}, delivered: ${ok}`);
  saveState("home", tasks);
}

async function runWeeklyCallout() {
  console.log(`[${ts()}] runWeeklyCallout starting`);
  for (const project of ['work', 'home']) {
    const staleItems = getStaleItems(project);
    console.log(`[${ts()}] runWeeklyCallout ${project}: ${staleItems.length} stale item(s)`);
    const message = formatWeeklyCallout(staleItems, project);
    if (message) {
      const ok = await deliver(message, { parse_mode: 'MarkdownV2' });
      console.log(`[${ts()}] runWeeklyCallout ${project} delivered: ${ok}`);
      await sleep(3000);
    }
  }
  console.log(`[${ts()}] runWeeklyCallout done`);
}

// --- Immediate runner ---

async function runNow(jobName) {
  migrateState();
  console.log(`[${ts()}] runNow: ${jobName}`);
  switch (jobName) {
    case "workDigest":
      await workDigest();
      break;
    case "workNag":
      await workNag("NOW");
      break;
    case "homeDigest":
      await homeDigest();
      break;
    case "homeEveningNag":
      await homeEveningNag();
      break;
    case "allWork":
      await workDigest();
      await sleep(3000);
      await workNag("NOW");
      break;
    case "allHome":
      await homeDigest();
      await sleep(3000);
      await homeEveningNag();
      break;
    case "weeklyCallout":
      await runWeeklyCallout();
      break;
    default:
      console.error(`Unknown job: "${jobName}". Options: workDigest, workNag, homeDigest, homeEveningNag, allWork, allHome, weeklyCallout`);
  }
}

// --- Scheduler ---

function startScheduler() {
  migrateState();
  const tz = { timezone: "Europe/London" };

  // 07:15 — home digest then morning home nag (3s gap so messages arrive in order)
  cron.schedule("15 7 * * *", async () => {
    await homeDigest();
    await sleep(3000);
    await homeNag();
  }, tz);

  // 07:30 — work digest
  cron.schedule("30 7 * * *", () => workDigest(), tz);

  // 07:45 — work nag
  cron.schedule("45 7 * * *", () => workNag("07:45"), tz);

  // 12:00 — work nag
  cron.schedule("0 12 * * *", () => workNag("12:00"), tz);

  // 16:00 — work evening nag (includes unprocessed nudge)
  cron.schedule("0 16 * * *", () => workNag("16:00", true), tz);

  // 18:00 — home evening nag
  cron.schedule("0 18 * * *", () => homeEveningNag(), tz);

  // Friday 16:30 — weekly staleness callout
  cron.schedule(config.WEEKLY_CALLOUT_SCHEDULE, () => runWeeklyCallout(), tz);

  console.log(`🚀 OpenClaw scheduler started - ${ts()}`);
  console.log(`📋 DRY_RUN: ${DRY_RUN}`);
  console.log(`⏰ Jobs scheduled: workDigest 07:30, workNag 07:45/12:00/16:00, homeDigest+Nag 07:15, homeEveningNag 18:00, weeklyCallout Fri 16:30`);
}

module.exports = { runNow, workDigest, workNag, homeDigest, homeEveningNag, saveState, deliver, readState, migrateState, getStaleItems, runWeeklyCallout, updateStalenessState };

if (require.main === module) {
  const jobName = process.argv[2];
  if (jobName) {
    runNow(jobName).catch(err => console.error(`[${ts()}] Fatal:`, err.message));
  } else {
    startScheduler();
  }
}
