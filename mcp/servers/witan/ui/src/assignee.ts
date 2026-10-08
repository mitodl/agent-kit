/**
 * The assignee filter: who holds a task, as one value per person.
 *
 * A claim writes a session qualifier after the owner (`<identity>#003625bb`), so
 * one person appears as several raw `assignee` values across tasks. Everything
 * here works on the normalized form, so one person is one checkbox and one
 * selection matches every session of theirs.
 */

/**
 * The route value that stands for "no assignee".
 *
 * Not an empty string: that is what `URLSearchParams` makes of a bare
 * `assignee=`, and not a plausible owner: identities are a person's name or a
 * worker's id, not the word "unassigned".
 */
export const UNASSIGNED = "unassigned";

/**
 * A trailing `#<session>`: the server's own rule (`SESSION_SUFFIX_RE` in
 * `witan/readiness.py`), anchored at the end and limited to a session id's
 * charset, so an identity that merely contains a `#` is not cut short.
 */
const SESSION_SUFFIX = /#[0-9A-Za-z_-]{1,64}$/;

/** The owner of an assignee value: the identity, without its session qualifier. */
export function normalizeAssignee(
	assignee: string | null | undefined,
): string | null {
	const owner = assignee?.replace(SESSION_SUFFIX, "").trim();
	return owner ? owner : null;
}

/**
 * Whether a task passes the selection.
 *
 * An empty selection passes everything. A held task is matched on its owner
 * whether or not its claim has lapsed: an expired lease still names who held
 * it, and showing it as unassigned would hide it from the person it is stale
 * for.
 */
export function matchesAssignee(
	task: { assignee: string | null },
	selected: readonly string[],
): boolean {
	if (selected.length === 0) {
		return true;
	}
	const owner = normalizeAssignee(task.assignee);
	return owner === null
		? selected.includes(UNASSIGNED)
		: selected.includes(owner);
}

/** `rows` narrowed to the selection. Returns the same array when it is empty. */
export function filterByAssignee<T extends { assignee: string | null }>(
	rows: T[],
	selected: readonly string[],
): T[] {
	return selected.length === 0
		? rows
		: rows.filter((row) => matchesAssignee(row, selected));
}

/**
 * The people to offer, sorted, from the tasks a view has loaded.
 *
 * Anything already selected is kept even when no loaded task carries it, so a
 * shared link to a person with no work in the current scope still shows its
 * own checkbox, ticked, rather than filtering to nothing with no way to see why.
 */
export function assigneeOptions(
	rows: readonly { assignee: string | null }[],
	selected: readonly string[],
): string[] {
	const owners = new Set(selected.filter((value) => value !== UNASSIGNED));
	for (const row of rows) {
		const owner = normalizeAssignee(row.assignee);
		if (owner) {
			owners.add(owner);
		}
	}
	return [...owners].sort((a, b) => a.localeCompare(b));
}
