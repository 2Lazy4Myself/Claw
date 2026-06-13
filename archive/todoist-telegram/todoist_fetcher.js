// Fetches and shapes Todoist tasks for the notification system.
// All data comes from Todoist REST API v1 via axios.

require("dotenv").config();
const axios = require("axios");

const API_BASE = "https://api.todoist.com/api/v1";

const SECTIONS = {
  work: {
    projectId: "6RmFxHccRw63CJ94",
    WAITING:     "6Rp3829965pCcVVW",  // "Waiting for"
    UNPROCESSED: "6Rp374jhgfgP3prW",  // "Unprocessed"
    TODAY:       "6Rmj8j24Mp77RwHW",  // "Today"
    NEXT_FEW:    "6Rmj8jq5vMwhrmfW",  // "Next 2-3 Days"
    THIS_WEEK:   "6Rmj8mJVcR9JhRpW",  // "This week" (lowercase w - exact Todoist name)
    NEXT_WEEK:   "6V63Gr28FH8H2q54",  // "Next Week"
    THIS_MONTH:  "6V63GxJh2mFj3MX4",  // "This Month"
  },
  home: {
    projectId: "6RmFxHWHv3f2p9Cr",
    WAITING:     "6gXj6J8gq3xH435J",  // "Waiting for"
    UNPROCESSED: "6gXj6jqqqGmvp6Vr",  // "Unprocessed"
    TODAY:       "6WMh74gr9RrqmqCJ",  // "Today"
    NEXT_FEW:    "6c39w73rGgjv965r",  // "Next 2-3 Days"
    THIS_WEEK:   "6gXj6JCHxM57pxxr",  // "This Week" (capital W - exact Todoist name)
    NEXT_WEEK:   "6gXj6JJ9hFcQCH9J",  // "Next Week"
    THIS_MONTH:  "6gXj6JPmV8xmcHVJ",  // "This Month"
  }
};

const IGNORED_SECTION_NAMES = ["Routines 🔁", "Inspiration ✨", "Later"];

// Build reverse map: sectionId -> section key name (e.g. "6Rmj8j24Mp77RwHW" -> "Today")
const SECTION_ID_TO_NAME = {};
for (const [projectKey, sections] of Object.entries(SECTIONS)) {
  for (const [key, id] of Object.entries(sections)) {
    if (key === "projectId") continue;
    // Use the human-readable key as a fallback; actual name resolved from API below
    SECTION_ID_TO_NAME[id] = key;
  }
}

// Maps section key (TODAY, WAITING, etc.) to a display name
const KEY_TO_DISPLAY = {
  TODAY:       "Today",
  WAITING:     "Waiting for",
  UNPROCESSED: "Unprocessed",
  NEXT_FEW:    "Next 2-3 Days",
  THIS_WEEK:   "This Week",
  NEXT_WEEK:   "Next Week",
  THIS_MONTH:  "This Month",
};

function authHeader() {
  return { Authorization: `Bearer ${process.env.TODOIST_API_TOKEN}` };
}

function todayString() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

// Returns all pages of results for a paginated endpoint
async function fetchAllPages(url, params = {}) {
  const results = [];
  let cursor = null;
  do {
    const response = await axios.get(url, {
      headers: authHeader(),
      params: cursor ? { ...params, cursor } : params,
    });
    const data = response.data;
    results.push(...(data.results || data));
    cursor = data.next_cursor || null;
  } while (cursor);
  return results;
}

// Returns the display name for a section ID within a project
function resolveSectionName(projectKey, sectionId) {
  const sections = SECTIONS[projectKey];
  for (const [key, id] of Object.entries(sections)) {
    if (key === "projectId") continue;
    if (id === sectionId) return KEY_TO_DISPLAY[key] || key;
  }
  return null; // unknown section
}

async function getTasksForProject(projectKey) {
  const project = SECTIONS[projectKey];
  if (!project) throw new Error(`Unknown project key: ${projectKey}`);

  try {
    const tasks = await fetchAllPages(`${API_BASE}/tasks`, {
      project_id: project.projectId,
    });

    return tasks
      .filter(t => !t.is_completed)
      .map(t => {
        const sectionName = resolveSectionName(projectKey, t.section_id);
        return {
          id:           t.id,
          content:      t.content,
          description:  t.description || "",
          due_date:     t.due ? t.due.date : null,
          section_id:   t.section_id,
          section_name: sectionName,
          labels:       t.labels || [],
          is_completed: false,
        };
      })
      .filter(t => !IGNORED_SECTION_NAMES.includes(t.section_name));
  } catch (err) {
    console.error(`[${new Date().toISOString()}] getTasksForProject(${projectKey}) failed:`, err.message);
    return [];
  }
}

async function getTasksBySection(projectKey, sectionId) {
  const tasks = await getTasksForProject(projectKey);
  return tasks.filter(t => t.section_id === sectionId);
}

async function getTodayAndOverdueTasks(projectKey) {
  const tasks = await getTasksForProject(projectKey);
  const today = todayString();
  const todaySectionId = SECTIONS[projectKey].TODAY;

  const seen = new Set();
  const results = [];

  for (const t of tasks) {
    const inTodaySection = t.section_id === todaySectionId;
    const isDueOrOverdue = t.due_date !== null && t.due_date <= today;

    if (inTodaySection || isDueOrOverdue) {
      if (!seen.has(t.id)) {
        seen.add(t.id);
        results.push(t);
      }
    }
  }

  return results;
}

function parseWaitingFor(task) {
  // RULE 1: labels
  const qualifying = task.labels.filter(l => l.startsWith("1") || l.startsWith("+"));
  if (qualifying.length > 0) {
    return qualifying
      .map(l => l.startsWith("1") ? l.slice(1) : l)
      .join(", ");
  }

  // RULE 2: scan title for person name
  const content = task.content || "";

  // Word after trigger words
  const triggerMatch = content.match(/(?:for|with|from|Catchup)\s+([A-Z][a-z]+)/);
  if (triggerMatch) return triggerMatch[1];

  // Last capitalised word in title (at least 2 chars, not all-caps acronym)
  const words = content.split(/\s+/);
  for (let i = words.length - 1; i >= 0; i--) {
    const w = words[i].replace(/[^a-zA-Z]/g, "");
    if (w.length >= 2 && /^[A-Z][a-z]+$/.test(w)) return w;
  }

  // RULE 3
  return null;
}

async function getWaitingForTasks(projectKey) {
  const tasks = await getTasksBySection(projectKey, SECTIONS[projectKey].WAITING);
  return tasks.map(task => ({ task, waitingFor: parseWaitingFor(task) }));
}

async function getUnprocessedTasks(projectKey) {
  return getTasksBySection(projectKey, SECTIONS[projectKey].UNPROCESSED);
}

module.exports = {
  getTasksForProject,
  getTasksBySection,
  getTodayAndOverdueTasks,
  parseWaitingFor,
  getWaitingForTasks,
  getUnprocessedTasks,
  SECTIONS,
};

if (require.main === module) {
  (async () => {
    console.log("\n=== getTasksForProject(work) ===");
    const all = await getTasksForProject("work");
    console.log(`Total tasks (excl. ignored sections): ${all.length}`);

    console.log("\n=== getTodayAndOverdueTasks(work) ===");
    const todayTasks = await getTodayAndOverdueTasks("work");
    if (todayTasks.length === 0) {
      console.log("None");
    } else {
      todayTasks.forEach(t => console.log(`  [${t.due_date || "no date"}] ${t.content}`));
    }

    console.log("\n=== getWaitingForTasks(work) ===");
    const waiting = await getWaitingForTasks("work");
    if (waiting.length === 0) {
      console.log("None");
    } else {
      waiting.forEach(({ task, waitingFor }) =>
        console.log(`  ${task.content}  →  waiting for: ${waitingFor ?? "(unknown)"}`)
      );
    }

    console.log("\n=== getUnprocessedTasks(work) ===");
    const unprocessed = await getUnprocessedTasks("work");
    console.log(`Unprocessed count: ${unprocessed.length}`);
  })();
}
