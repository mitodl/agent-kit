import { html, nothing, type TemplateResult } from "lit-html";
import { absolute, ago, isLinkable, repoLabel } from "../format.js";
import { type Route, routeHref } from "../route.js";
import type { CodeBranchRef, TaskDetail } from "../types.js";
import { slugLinks, uriFact } from "./parts.js";

/**
 * The task detail pane (spec §6.3).
 *
 * This is the read the CLI cannot serve at all today, and the reason the panel
 * is worth building: every field on the Task node, its comment thread, and its
 * graph neighbours as links rather than slugs.
 *
 * ★ EVERY SECTION IS CONDITIONAL, and the task's acceptance criterion says why:
 * a task with no comments, no blockers and no project has to render cleanly
 * rather than showing empty scaffolding. A fixed layout with eight "—"
 * placeholders makes the common small task unreadable.
 */
export function taskDetail(
	task: TaskDetail,
	route: Route,
	now = Date.now(),
): TemplateResult {
	return html`
    <div class="task-detail">
      <p class="crumbs">
        <code>${task.slug}</code>
        <span class="badge">${task.type}</span>
        <span class="badge status-${task.status}">${task.status}</span>
        <span class="badge priority">${task.priority}</span>
        ${
					task.lease_expired
						? html`<span class="badge stale-claim">claim lapsed</span>`
						: nothing
				}
      </p>

      <dl class="facts">
        <dt>Repo</dt>
        <dd>
          ${
						task.repo
							? html`<a href=${routeHref(route, { repo: task.repo })}
                  >${repoLabel(task.repo)}</a
                >`
							: "—"
					}
        </dd>
        <dt>Assignee</dt>
        <dd>${task.assignee ?? "unassigned"}</dd>
        <dt>Author</dt>
        <dd>${task.author ?? "—"}</dd>
        <dt>Created</dt>
        <dd title=${absolute(task.created_at)}>${ago(task.created_at, now)}</dd>
        <dt>Updated</dt>
        <dd title=${absolute(task.updated_at)}>${ago(task.updated_at, now)}</dd>
        ${
					task.claimed_at
						? html`<dt>Claimed</dt>
              <dd title=${absolute(task.claimed_at)}>
                ${ago(task.claimed_at, now)}
              </dd>`
						: nothing
				}
        ${
					task.closed_at
						? html`<dt>Closed</dt>
              <dd title=${absolute(task.closed_at)}>${ago(task.closed_at, now)}</dd>`
						: nothing
				}
        ${uriFact("External", task.external_uri)}
        ${
					task.tags?.length
						? html`<dt>Tags</dt>
              <dd>${task.tags.join(", ")}</dd>`
						: nothing
				}
      </dl>

      ${prose("Description", task.description)}
      ${prose("Resolution", task.resolution)}
      ${neighbours(task, route)}
      ${symbolRefs(task)}
      ${comments(task, now)}
    </div>
  `;
}

function prose(
	heading: string,
	body: string | null,
): TemplateResult | typeof nothing {
	if (!body) {
		return nothing;
	}
	// Rendered as text, not markdown: descriptions are agent-written and
	// untrusted, and `white-space: pre-wrap` in the stylesheet keeps their
	// paragraph breaks without an HTML parse.
	return html`
    <section class="prose">
      <h3>${heading}</h3>
      <p>${body}</p>
    </section>
  `;
}

/**
 * The task's graph neighbours, as links into the panel or the project view.
 *
 * `blocks` is the inverse of `blocked_by` and only exists because the read-gap
 * task added it; without it "what does closing this unblock" was a question
 * the UI could not answer at all. `branches` is the CodeBranch carrying the
 * work, which is how a stale claim gets traced to an actual checkout.
 */
function neighbours(
	task: TaskDetail,
	route: Route,
): TemplateResult | typeof nothing {
	const blockedBy = task.blocked_by ?? [];
	const hasAny =
		blockedBy.length > 0 ||
		task.blocks.length > 0 ||
		task.children.length > 0 ||
		task.parent_slug !== null ||
		task.project_slug !== null ||
		task.branches.length > 0;
	if (!hasAny) {
		return nothing;
	}
	return html`
    <section class="edges">
      <h3>Graph</h3>
      ${
				task.project_slug
					? html`<p>
              <strong>Project</strong>:
              <a
                href=${routeHref(route, {
									view: "projects",
									project: task.project_slug,
									slug: null,
								})}
                ><code>${task.project_slug}</code></a
              >
            </p>`
					: nothing
			}
      ${
				task.parent_slug
					? html`<p>
              <strong>Parent</strong>:
              <a href=${routeHref(route, { slug: task.parent_slug })}
                ><code>${task.parent_slug}</code></a
              >
            </p>`
					: nothing
			}
      ${slugLinks("Blocked by", blockedBy, route, (slug) => ({ slug }))}
      ${slugLinks("Blocks", task.blocks, route, (slug) => ({ slug }))}
      ${
				task.children.length
					? html`<p><strong>Children</strong></p>
              <ul class="children">
                ${task.children.map(
									(child) => html`
                    <li>
                      <span class="badge status-${child.status}"
                        >${child.status}</span
                      >
                      <a href=${routeHref(route, { slug: child.slug })}
                        >${child.title}</a
                      >
                    </li>
                  `,
								)}
              </ul>`
					: nothing
			}
      ${
				task.branches.length
					? html`<p><strong>Branches</strong></p>
              <ul class="branches">
                ${task.branches.map(branchItem)}
              </ul>`
					: nothing
			}
    </section>
  `;
}

/**
 * One CodeBranch, linked to the branch on the forge.
 *
 * The branch is how a stale claim gets traced to an actual checkout, so the
 * useful thing to do with it is open it. `repo` is a canonical repo URI and
 * `branch` a branch name, which is enough to build a `/tree/` URL; a repo URI
 * that is not a URL (or a forge that does not use that path shape) falls back
 * to text rather than guessing, and `isLinkable` is what decides.
 *
 * `updated_at` is shown because it is the field that says whether the branch
 * is still being worked. `slug` goes in the `title` rather than the text: it
 * is `<repo>|<branch>`, a composite of the two values already on the line, and
 * rendering it again put a 60-character id beside the name it repeats.
 */
function branchItem(branch: CodeBranchRef): TemplateResult {
	const href = isLinkable(branch.repo)
		? `${branch.repo.replace(/\.git$/, "").replace(/\/$/, "")}/tree/${branch.branch
				.split("/")
				.map(encodeURIComponent)
				.join("/")}`
		: null;
	return html`
    <li title=${branch.slug}>
      ${
				href
					? html`<a href=${href}><code>${branch.branch}</code></a>`
					: html`<code>${branch.branch}</code>`
			}
      <span class="muted" title=${branch.repo}>${repoLabel(branch.repo)}</span>
      ${branch.status ? html`<span class="badge">${branch.status}</span>` : nothing}
      ${
				branch.updated_at
					? html`<span class="muted" title=${absolute(branch.updated_at)}
              >${ago(branch.updated_at)}</span
            >`
					: nothing
			}
    </li>
  `;
}

function symbolRefs(task: TaskDetail): TemplateResult | typeof nothing {
	if (!task.symbol_refs?.length) {
		return nothing;
	}
	return html`
    <section class="edges">
      <h3>Symbols</h3>
      <ul class="symbols">
        ${task.symbol_refs.map((ref) => html`<li><code>${ref}</code></li>`)}
      </ul>
    </section>
  `;
}

/**
 * The comment thread, oldest first.
 *
 * Oldest first because that is the order `task_get` returns and the order the
 * corrections read in: a comment is how one agent tells the next that the
 * task's stated premise is wrong, and reversing them puts the correction
 * before the thing it corrects.
 */
function comments(
	task: TaskDetail,
	now: number,
): TemplateResult | typeof nothing {
	if (task.comments.length === 0) {
		return nothing;
	}
	return html`
    <section>
      <h3>Comments <span class="count">${task.comments.length}</span></h3>
      <ul class="comments">
        ${task.comments.map(
					(comment) => html`
            <li>
              <p class="comment-head">
                <strong>${comment.author}</strong>
                <span title=${absolute(comment.created_at)}
                  >${ago(comment.created_at, now)}</span
                >
              </p>
              <p class="prose">${comment.body}</p>
            </li>
          `,
				)}
      </ul>
    </section>
  `;
}

/** What the panel shows for a slug the graph does not have. */
export function taskMissing(slug: string): TemplateResult {
	return html`
    <p class="empty">
      No task <code>${slug}</code> in this graph. The link may be stale, or
      point at another target.
    </p>
  `;
}
