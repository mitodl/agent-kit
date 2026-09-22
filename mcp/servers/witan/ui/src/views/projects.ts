import { html, nothing, type TemplateResult } from "lit-html";
import { emptyBox } from "../chrome.js";
import { absolute, ago, repoLabel } from "../format.js";
import { type Route, routeHref } from "../route.js";
import type {
	LastSession,
	ProjectStatus,
	TaskRow,
	TaskStatus,
	WorkflowProjectDetail,
	WorkflowProjectSummary,
	WorkflowSession,
} from "../types.js";
import { slugLinks, uriFact } from "./parts.js";

/**
 * The Projects view: the list, and the rollup for one project (spec §6.2).
 *
 * Both are pure renderers over data the app has already read. Nothing here
 * calls a tool, so every shape below can be tested against the recorded
 * fixtures with no server and no transport.
 */

/** Everything the rollup draws, read as four calls (spec §6.2). */
export interface Rollup {
	detail: WorkflowProjectDetail | null;
	status: ProjectStatus | null;
	tasks: TaskRow[];
	sessions: WorkflowSession[];
}

export function projectList(
	projects: WorkflowProjectSummary[],
	route: Route,
): TemplateResult {
	if (projects.length === 0) {
		return emptyBox(
			route.repo
				? `No projects in ${repoLabel(route.repo)}.`
				: "No projects in the graph.",
		);
	}
	return html`
    <table class="rows">
      <thead>
        <tr>
          <th>Project</th>
          <th>Phase</th>
          <th>Status</th>
          <th>Repos</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>
        ${projects.map(
					(project) => html`
            <tr>
              <td>
                <a href=${routeHref(route, { project: project.slug })}
                  >${project.title}</a
                >
                ${
									project.blocked_by?.length
										? html`<span class="badge blocked"
                        >blocked by ${project.blocked_by.length}</span
                      >`
										: nothing
								}
              </td>
              <td>${project.phase}</td>
              <td>${project.status}</td>
              <td>${(project.repos ?? []).map(repoLabel).join(", ") || "—"}</td>
              <td title=${absolute(project.updated_at)}>
                ${ago(project.updated_at)}
              </td>
            </tr>
          `,
				)}
      </tbody>
    </table>
  `;
}

/**
 * One project, rolled up.
 *
 * The four reads are drawn separately rather than merged: `workflow_project_get`
 * and `workflow_project_status` return DIFFERENT projections of the same node
 * (the rollup's `project` is six fields; the detail is fifteen), and folding
 * them into one object is what made an earlier cut unable to reach
 * `description` and `blocks`.
 */
export function projectRollup(
	rollup: Rollup,
	route: Route,
	now = Date.now(),
): TemplateResult {
	const { detail, status } = rollup;
	if (!detail) {
		// A slug that resolves to nothing is a stale link, not a failure: the
		// same case `task_get` returns `null` for.
		return html`
      <p class="empty">
        No project <code>${route.project}</code> in this graph.
        <a href=${routeHref(route, { project: null })}>Back to all projects</a>.
      </p>
    `;
	}

	const tasks = rollup.tasks.filter(
		(task) => route.closed || task.status !== "closed",
	);

	return html`
    <article class="rollup">
      <header>
        <p class="crumbs">
          <a href=${routeHref(route, { project: null })}>Projects</a> /
          <code>${detail.slug}</code>
        </p>
        <h2>${detail.title}</h2>
        <dl class="facts">
          <dt>Phase</dt>
          <dd>${detail.phase}</dd>
          <dt>Status</dt>
          <dd>${detail.status}</dd>
          <dt>Repos</dt>
          <dd>
            ${
							/*
							 * The links drop the project, the same way the shell's repo
							 * filter does. Keeping it would narrow the task table to one
							 * repo while `workflow_project_status`'s counts and ready list
							 * stayed project-wide (they take no `repo`), so the header
							 * would read "3 of 12 open" over a table of 3 and the Ready
							 * list above would name tasks absent from it.
							 */
							(detail.repos ?? []).length === 0
								? "—"
								: (detail.repos ?? []).map(
										(repo, index) => html`${index > 0 ? ", " : ""}
                      <a
                        href=${routeHref(route, { repo, project: null, slug: null })}
                        title=${repo}
                        >${repoLabel(repo)}</a
                      >`,
									)
						}
          </dd>
          <dt>Author</dt>
          <dd>${detail.author ?? "—"}</dd>
          <dt>Created</dt>
          <dd title=${absolute(detail.created_at)}>${ago(detail.created_at, now)}</dd>
          <dt>Updated</dt>
          <dd title=${absolute(detail.updated_at)}>${ago(detail.updated_at, now)}</dd>
          ${
						detail.completed_at
							? html`<dt>Completed</dt>
                  <dd title=${absolute(detail.completed_at)}>
                    ${ago(detail.completed_at, now)}
                  </dd>`
							: nothing
					}
          ${uriFact("Issue", detail.github_issue)}
          ${uriFact("PR", detail.github_pr)}
          ${
						detail.tags?.length
							? html`<dt>Tags</dt>
                  <dd>${detail.tags.join(", ")}</dd>`
							: nothing
					}
        </dl>
      </header>

      ${
				detail.description
					? html`<section class="prose">
              <h3>Description</h3>
              <p>${detail.description}</p>
            </section>`
					: nothing
			}

      ${projectEdges(detail, status, route)}
      ${readyTasks(status, route)}

      <section>
        <h3>
          Tasks <span class="count">${tasks.length} shown</span>
          ${
						/*
						 * The open count is a separate figure, not a denominator.
						 * "${tasks.length} of ${open_tasks} open" read as a truncation
						 * indicator: with Closed unticked the two numbers are the same,
						 * and with it ticked the first is the larger of the pair. Both
						 * carry their own word so two bare numbers never sit adjacent.
						 */
						status
							? html`<span class="count"
                  >· ${status.counts.open_tasks} open</span
                >`
							: nothing
					}
        </h3>
        ${taskTable(tasks, route, now)}
      </section>

      <section>
        <h3>Sessions <span class="count">${rollup.sessions.length}</span></h3>
        ${sessionList(rollup.sessions, now)}
      </section>
    </article>
  `;
}

/**
 * What this project waits on and what waits on it, plus the rollup's blockers.
 *
 * `blocked_by`/`blocks` are project-to-project edges from `workflow_project_get`;
 * `status.blockers` is the rollup's own list. They are shown together because
 * "why is this stuck" is one question, and separately labelled because they
 * come from different edges.
 */
function projectEdges(
	detail: WorkflowProjectDetail,
	status: ProjectStatus | null,
	route: Route,
): TemplateResult | typeof nothing {
	const blockedBy = detail.blocked_by ?? [];
	const blockers = status?.blockers ?? [];
	if (
		blockedBy.length === 0 &&
		detail.blocks.length === 0 &&
		blockers.length === 0
	) {
		return nothing;
	}
	return html`
    <section class="edges">
      <h3>Dependencies</h3>
      ${slugLinks("Blocked by", blockedBy, route, (project) => ({ project }))}
      ${slugLinks("Blocks", detail.blocks, route, (project) => ({ project }))}
      ${
				blockers.length
					? html`<p><strong>Blockers</strong>: ${blockers.join(", ")}</p>`
					: nothing
			}
    </section>
  `;
}

/**
 * The rollup's ready tasks.
 *
 * Drawn from `workflow_project_status`, not recomputed from the task list: the
 * readiness rule lives on the server and a second copy of it in the browser is
 * a copy that drifts. `ready_truncated` is surfaced because the tool caps the
 * list while `counts.ready` keeps counting, so a page that showed only the
 * rows would under-report ready work.
 */
function readyTasks(
	status: ProjectStatus | null,
	route: Route,
): TemplateResult | typeof nothing {
	if (!status) {
		return nothing;
	}
	return html`
    <section>
      <h3>Ready <span class="count">${status.counts.ready}</span></h3>
      ${
				status.ready_tasks.length === 0
					? emptyBox("Nothing is ready to pick up.")
					: html`
            <ul class="ready">
              ${status.ready_tasks.map(
								(task) => html`
                  <li>
                    <span class="badge priority">${task.priority}</span>
                    <a href=${routeHref(route, { slug: task.slug })}
                      >${task.title}</a
                    >
                    ${
											task.assignee
												? html`<span class="assignee">${task.assignee}</span>`
												: nothing
										}
                    ${
											task.lease_expired
												? html`<span class="badge stale-claim"
                            >claim lapsed</span
                          >`
												: nothing
										}
                  </li>
                `,
							)}
            </ul>
            ${
							status.ready_truncated
								? html`<p class="note">
                    The tool capped this list; ${status.counts.ready} tasks are
                    ready.
                  </p>`
								: nothing
						}
          `
			}
    </section>
  `;
}

/**
 * The rollup as `workflow_project_status` alone can draw it, for that tool's
 * widget (spec §7.1).
 *
 * The page's rollup reads four tools; a widget is handed one result and calls
 * nothing back, so this draws the status projection and says nothing it did
 * not read: no description, no task table, one session rather than the
 * history. `null` is the tool's answer for a slug that does not exist.
 */
export function statusRollup(
	status: ProjectStatus | null,
	route: Route,
	now = Date.now(),
): TemplateResult {
	if (!status) {
		return emptyBox("No such project in this graph.");
	}
	const { project } = status;
	return html`
    <article class="rollup">
      <header>
        <p class="crumbs"><code>${project.slug}</code></p>
        <h2>${project.title}</h2>
        <dl class="facts">
          <dt>Phase</dt>
          <dd>${project.phase}</dd>
          <dt>Status</dt>
          <dd>${project.status}</dd>
          <dt>Repos</dt>
          <dd>${(project.repos ?? []).map(repoLabel).join(", ") || "—"}</dd>
          <dt>Open tasks</dt>
          <dd>${status.counts.open_tasks}</dd>
          ${uriFact("PR", project.github_pr)}
        </dl>
      </header>
      ${
				status.blockers.length
					? html`<section class="edges">
              <h3>Blockers</h3>
              <p>${status.blockers.join(", ")}</p>
            </section>`
					: nothing
			}
      ${readyTasks(status, route)}
      <section>
        <h3>Last session</h3>
        ${lastSession(status.last_session, now)}
      </section>
    </article>
  `;
}

function lastSession(session: LastSession | null, now: number): TemplateResult {
	if (!session) {
		return emptyBox("No sessions recorded against this project.");
	}
	return html`
    <ul class="sessions">
      <li>
        <p class="session-head">
          <code>${session.slug}</code>
          <span class="badge">${session.phase}</span>
          ${
						session.ended_at
							? html`<span title=${absolute(session.ended_at)}
                  >ended ${ago(session.ended_at, now)}</span
                >`
							: html`<span class="badge open">open</span>`
					}
        </p>
        ${
					session.summary
						? html`<p class="summary">${session.summary}</p>`
						: html`<p class="summary muted">No summary.</p>`
				}
      </li>
    </ul>
  `;
}

const STATUS_ORDER: TaskStatus[] = ["in_progress", "open", "blocked", "closed"];

const STATUS_HEADINGS: Record<TaskStatus, string> = {
	in_progress: "In progress",
	open: "Open",
	blocked: "Blocked",
	closed: "Closed",
};

/**
 * `task_list`'s rows grouped by status, for that tool's widget (spec §7.1).
 *
 * Grouped on the row's own status word, which is all a `task_list` result
 * carries. That is not the board's rule: an `open` task with an open blocker
 * lands under Open here, because telling it apart takes a `task_ready` read
 * this widget is not handed. Empty groups are left out rather than drawn with
 * an empty message, since the call may have filtered to one status.
 */
export function taskGroups(
	tasks: TaskRow[],
	route: Route,
	now = Date.now(),
): TemplateResult {
	if (tasks.length === 0) {
		return emptyBox("No tasks matched.");
	}
	return html`${STATUS_ORDER.map((status) => {
		const group = tasks.filter((task) => task.status === status);
		return group.length === 0
			? nothing
			: html`<section>
            <h3>${STATUS_HEADINGS[status]} <span class="count">${group.length}</span></h3>
            ${taskTable(group, route, now)}
          </section>`;
	})}`;
}

export function taskTable(
	tasks: TaskRow[],
	route: Route,
	now = Date.now(),
): TemplateResult {
	if (tasks.length === 0) {
		return emptyBox(
			route.closed
				? "No tasks on this project."
				: "No open tasks. Tick Closed to see finished work.",
		);
	}
	return html`
    <table class="rows">
      <thead>
        <tr>
          <th>Task</th>
          <th>Type</th>
          <th>Status</th>
          <th>Pri</th>
          <th>Assignee</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>
        ${tasks.map(
					(task) => html`
            <tr class=${task.status === "closed" ? "closed" : ""}>
              <td>
                <a href=${routeHref(route, { slug: task.slug })}>${task.title}</a>
                ${
									task.blocked_by?.length
										? html`<span class="badge blocked"
                        >${task.blocked_by.length} blocker${
													task.blocked_by.length === 1 ? "" : "s"
												}</span
                      >`
										: nothing
								}
              </td>
              <td>${task.type}</td>
              <td>
                ${task.status}
                ${
									task.lease_expired
										? html`<span class="badge stale-claim">lapsed</span>`
										: nothing
								}
              </td>
              <td>${task.priority}</td>
              <td>${task.assignee ?? "—"}</td>
              <td title=${absolute(task.updated_at)}>${ago(task.updated_at, now)}</td>
            </tr>
          `,
				)}
      </tbody>
    </table>
  `;
}

/**
 * The session history, newest first.
 *
 * The summaries are the handoff notes between agent sessions, which is the
 * part of a project's history nothing else in the UI shows. The CURRENT one
 * is what a person opens a rollup to read, so it goes at the top; the tool
 * returns them ascending (`read.gq`: `order { $s.started_at asc }`), which on
 * a long-running project buries it under everything that came before.
 */
function sessionList(
	sessions: WorkflowSession[],
	now = Date.now(),
): TemplateResult {
	if (sessions.length === 0) {
		return emptyBox("No sessions recorded against this project.");
	}
	return html`
    <ul class="sessions">
      ${[...sessions].reverse().map(
				(session) => html`
          <li>
            <p class="session-head">
              <code>${session.slug}</code>
              <span class="badge">${session.phase}</span>
              <span title=${absolute(session.started_at)}
                >started ${ago(session.started_at, now)}</span
              >
              ${
								session.ended_at
									? html`<span title=${absolute(session.ended_at)}
                      >ended ${ago(session.ended_at, now)}</span
                    >`
									: html`<span class="badge open">open</span>`
							}
            </p>
            ${
							session.summary
								? html`<p class="summary">${session.summary}</p>`
								: html`<p class="summary muted">No summary.</p>`
						}
          </li>
        `,
			)}
    </ul>
  `;
}
