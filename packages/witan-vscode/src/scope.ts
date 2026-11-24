// What the work tree lists: one repo scope or one project. The server decides
// which rows a scope means (`witan.listReadyTasks` and the other work lists);
// this module only names the choices, sends them, and labels the answer.

export type RepoScope = "repo" | "repo+unfiled" | "all";

export type Scope = { scope: RepoScope } | { project: string; title: string };

/** The scope a list command answers it applied, `undefined` from a server
 * older than scopes. */
export interface Applied {
	scope?: string;
	project?: string | null;
}

export const DEFAULT_SCOPE: Scope = { scope: "repo" };

export const REPO_SCOPES: ReadonlyArray<{ scope: RepoScope; label: string }> = [
	{ scope: "repo", label: "This repo" },
	{ scope: "repo+unfiled", label: "This repo + unfiled" },
	{ scope: "all", label: "All repos" },
];

/** The arguments a work-list command takes for `scope`. */
export function scopeArgs(scope: Scope): Record<string, string> {
	return "project" in scope
		? { project: scope.project }
		: { scope: scope.scope };
}

/** What a server that honours `scope` answers it applied: the view's label
 * until the first list comes back, so a failed load does not keep the last. */
export function requested(scope: Scope): Applied {
	return "project" in scope
		? { scope: "project", project: scope.project }
		: { scope: scope.scope, project: null };
}

/** The view's description for what the server applied. `title` names the
 * project the person picked, when it is the one applied. */
export function appliedLabel(applied: Applied, picked: Scope): string {
	if (applied.scope === "project" && applied.project) {
		const title =
			"project" in picked && picked.project === applied.project
				? picked.title
				: applied.project;
		return `Project: ${title}`;
	}
	// A server older than scopes ignores the argument and answers what every
	// list did before: the folder's repo plus what is filed under none.
	const scope = applied.scope ?? "repo+unfiled";
	return REPO_SCOPES.find((s) => s.scope === scope)?.label ?? scope;
}

/** A scope read back from workspace state, or the default when it is not one. */
export function restoreScope(stored: unknown): Scope {
	if (typeof stored !== "object" || stored === null) {
		return DEFAULT_SCOPE;
	}
	const value = stored as Record<string, unknown>;
	if (typeof value.project === "string" && typeof value.title === "string") {
		return { project: value.project, title: value.title };
	}
	const repo = REPO_SCOPES.find((s) => s.scope === value.scope);
	return repo ? { scope: repo.scope } : DEFAULT_SCOPE;
}
