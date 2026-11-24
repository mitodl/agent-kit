// The ready-work tree: what the server's list commands return, per workspace
// folder. The spec carries no session id, so work in flight is the person's
// own in_progress tasks (`witan.listMyWork`), not what a launch picked up.

import * as vscode from "vscode";
import { type Applied, DEFAULT_SCOPE, type Scope, scopeArgs } from "./scope";

export type ServerCommand = (
	command: string,
	args: Record<string, unknown>,
) => Promise<unknown>;

export interface TaskRow {
	slug: string;
	title: string;
	status?: string;
	priority?: string;
	type?: string;
	lease_expired?: boolean;
}

export interface ProjectRow {
	slug: string;
	title: string;
	phase?: string;
	status?: string;
}

interface Lists {
	applied: Applied;
	repo: string;
	branch: string | null;
	ready: TaskRow[];
	forBranch: TaskRow[];
	mine: TaskRow[];
	projects: ProjectRow[];
}

export class FolderNode {
	lists?: Promise<Lists>;
	constructor(readonly folder: vscode.WorkspaceFolder) {}
}

export class SectionNode {
	constructor(
		readonly folder: vscode.WorkspaceFolder,
		readonly label: string,
		readonly rows: TaskRow[] | ProjectRow[],
		readonly kind: "task" | "project",
	) {}
}

export class TaskNode {
	constructor(
		readonly folder: vscode.WorkspaceFolder,
		readonly task: TaskRow,
	) {}
}

export class ProjectNode {
	constructor(
		readonly folder: vscode.WorkspaceFolder,
		readonly project: ProjectRow,
	) {}
}

export class MessageNode {
	constructor(
		readonly message: string,
		readonly error: boolean,
	) {}
}

export type WorkNode =
	| FolderNode
	| SectionNode
	| TaskNode
	| ProjectNode
	| MessageNode;

export class WorkTreeProvider implements vscode.TreeDataProvider<WorkNode> {
	private readonly changed = new vscode.EventEmitter<WorkNode | undefined>();
	readonly onDidChangeTreeData = this.changed.event;
	private readonly applied = new vscode.EventEmitter<Applied>();
	/** The scope the server answered it applied, on each folder's load. */
	readonly onDidApplyScope = this.applied.event;
	private folders = new Map<string, FolderNode>();

	constructor(
		private readonly request: ServerCommand,
		public scope: Scope = DEFAULT_SCOPE,
	) {}

	refresh(): void {
		this.folders.clear();
		this.changed.fire(undefined);
	}

	setScope(scope: Scope): void {
		this.scope = scope;
		this.refresh();
	}

	async getChildren(node?: WorkNode): Promise<WorkNode[]> {
		if (node === undefined) {
			const folders = vscode.workspace.workspaceFolders ?? [];
			if (folders.length === 0) {
				return [new MessageNode("Open a folder to see its witan work.", false)];
			}
			const nodes = folders.map((f) => this.folderNode(f));
			const [only] = nodes;
			return only && nodes.length === 1 ? this.sections(only) : nodes;
		}
		if (node instanceof FolderNode) {
			return this.sections(node);
		}
		if (node instanceof SectionNode) {
			if (node.rows.length === 0) {
				return [new MessageNode("Nothing here.", false)];
			}
			return node.kind === "task"
				? (node.rows as TaskRow[]).map((t) => new TaskNode(node.folder, t))
				: (node.rows as ProjectRow[]).map(
						(p) => new ProjectNode(node.folder, p),
					);
		}
		return [];
	}

	getTreeItem(node: WorkNode): vscode.TreeItem {
		if (node instanceof FolderNode) {
			const item = new vscode.TreeItem(
				node.folder.name,
				vscode.TreeItemCollapsibleState.Expanded,
			);
			item.iconPath = vscode.ThemeIcon.Folder;
			return item;
		}
		if (node instanceof SectionNode) {
			const item = new vscode.TreeItem(
				node.label,
				node.rows.length === 0
					? vscode.TreeItemCollapsibleState.Collapsed
					: vscode.TreeItemCollapsibleState.Expanded,
			);
			item.description = String(node.rows.length);
			return item;
		}
		if (node instanceof TaskNode) {
			const { task } = node;
			const item = new vscode.TreeItem(task.title);
			item.description = [task.priority, task.slug].filter(Boolean).join(" · ");
			item.tooltip = [
				task.title,
				task.slug,
				[task.type, task.status, task.priority].filter(Boolean).join(", "),
				task.lease_expired ? "Lease expired: reclaimable." : "",
			]
				.filter(Boolean)
				.join("\n");
			item.contextValue = "witanTask";
			item.iconPath = new vscode.ThemeIcon(
				task.status === "in_progress" ? "sync" : "circle-large-outline",
			);
			return item;
		}
		if (node instanceof ProjectNode) {
			const { project } = node;
			const item = new vscode.TreeItem(project.title);
			item.description = [project.phase, project.slug]
				.filter(Boolean)
				.join(" · ");
			item.tooltip = `${project.title}\n${project.slug}`;
			item.contextValue = "witanProject";
			item.iconPath = new vscode.ThemeIcon("project");
			return item;
		}
		const item = new vscode.TreeItem(node.message);
		item.iconPath = new vscode.ThemeIcon(node.error ? "error" : "info");
		return item;
	}

	private folderNode(folder: vscode.WorkspaceFolder): FolderNode {
		const key = folder.uri.toString();
		let node = this.folders.get(key);
		if (node === undefined) {
			node = new FolderNode(folder);
			this.folders.set(key, node);
		}
		return node;
	}

	private async sections(node: FolderNode): Promise<WorkNode[]> {
		node.lists ??= this.load(node.folder);
		let lists: Lists;
		try {
			lists = await node.lists;
		} catch (err) {
			// Not cached: the next refresh asks again.
			node.lists = undefined;
			return [new MessageNode(errorMessage(err), true)];
		}
		// A load a refresh or scope change has since replaced would relabel the
		// view with the scope it was asked for.
		if (this.folders.get(node.folder.uri.toString()) === node) {
			this.applied.fire(lists.applied);
		}
		const { folder } = node;
		const sections: WorkNode[] = [
			new SectionNode(folder, "In progress (mine)", lists.mine, "task"),
		];
		if (lists.branch && lists.forBranch.length > 0) {
			sections.push(
				new SectionNode(folder, `On ${lists.branch}`, lists.forBranch, "task"),
			);
		}
		sections.push(
			new SectionNode(folder, "Ready", lists.ready, "task"),
			new SectionNode(folder, "Projects", lists.projects, "project"),
		);
		return sections;
	}

	private async load(folder: vscode.WorkspaceFolder): Promise<Lists> {
		const args = { uri: folder.uri.toString(), ...scopeArgs(this.scope) };
		const [ready, mine, projects] = (await Promise.all([
			this.request("witan.listReadyTasks", args),
			this.request("witan.listMyWork", args),
			this.request("witan.listProjects", args),
		])) as [
			Applied & {
				repo: string;
				branch: string | null;
				ready: TaskRow[];
				for_branch: TaskRow[];
			},
			{ tasks: TaskRow[] },
			{ projects: ProjectRow[] },
		];
		return {
			applied: { scope: ready.scope, project: ready.project },
			repo: ready.repo,
			branch: ready.branch,
			ready: ready.ready,
			forBranch: ready.for_branch,
			mine: mine.tasks,
			projects: projects.projects,
		};
	}
}

export function errorMessage(err: unknown): string {
	return err instanceof Error ? err.message : String(err);
}
