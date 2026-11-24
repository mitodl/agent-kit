// The ready-work tree: what the server's list commands return, per workspace
// folder. The spec carries no session id, so work in flight is the person's
// own in_progress tasks (`witan.listMyWork`), not what a launch picked up.

import * as vscode from "vscode";
import {
	type Applied,
	appliedLabel,
	DEFAULT_SCOPE,
	requested,
	type Scope,
	scopeArgs,
} from "./scope";

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

type ListName = "ready" | "mine" | "projects";

interface Lists {
	/** Per list: an older deployment can answer each in a different scope. */
	applied: Record<ListName, Applied>;
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
		/** `undefined` for the branch's tasks, which no scope changes. */
		readonly applied: Applied | undefined,
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
	private readonly applied = new vscode.EventEmitter<Applied[]>();
	/** Every scope the server answered it applied, per folder and list, on
	 * each folder's load. */
	readonly onDidApplyScope = this.applied.event;
	private folders = new Map<string, FolderNode>();
	private appliedLists = new Map<string, Applied>();

	constructor(
		private readonly request: ServerCommand,
		public scope: Scope = DEFAULT_SCOPE,
	) {}

	refresh(): void {
		this.folders.clear();
		this.appliedLists.clear();
		this.changed.fire(undefined);
	}

	setScope(scope: Scope): void {
		this.scope = scope;
		this.refresh();
	}

	async getChildren(node?: WorkNode): Promise<WorkNode[]> {
		if (node === undefined) {
			const folders = this.listedFolders();
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
			if (node.applied === undefined) {
				item.tooltip = "This branch's tasks, whatever the scope.";
				return item;
			}
			// Named on the section only where it is not the scope picked, which
			// the view's description already shows.
			const label = appliedLabel(node.applied, this.scope);
			if (label !== appliedLabel(requested(this.scope), this.scope)) {
				item.description += ` · ${label}`;
			}
			item.tooltip = `Scope: ${label}`;
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

	/** The folders whose work the view lists. A project is listed once,
	 * through the folder it was picked from: a folder on the same graph would
	 * list the same rows, and one on another graph none. If that folder is no
	 * longer open, each open folder asks its own route. */
	private listedFolders(): readonly vscode.WorkspaceFolder[] {
		const folders = vscode.workspace.workspaceFolders ?? [];
		const { scope } = this;
		if (!("project" in scope)) {
			return folders;
		}
		const source = folders.find((f) => f.uri.toString() === scope.folder);
		return source ? [source] : folders;
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
		const { folder } = node;
		const key = folder.uri.toString();
		if (this.folders.get(key) === node) {
			for (const [list, applied] of Object.entries(lists.applied)) {
				this.appliedLists.set(`${key} ${list}`, applied);
			}
			this.applied.fire([...this.appliedLists.values()]);
		}
		const { applied } = lists;
		const sections: WorkNode[] = [
			new SectionNode(
				folder,
				"In progress (mine)",
				lists.mine,
				"task",
				applied.mine,
			),
		];
		if (lists.branch && lists.forBranch.length > 0) {
			sections.push(
				new SectionNode(
					folder,
					`On ${lists.branch}`,
					lists.forBranch,
					"task",
					undefined,
				),
			);
		}
		sections.push(
			new SectionNode(folder, "Ready", lists.ready, "task", applied.ready),
			new SectionNode(
				folder,
				"Projects",
				lists.projects,
				"project",
				applied.projects,
			),
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
			Applied & { tasks: TaskRow[] },
			Applied & { projects: ProjectRow[] },
		];
		const applied = ({ scope, project }: Applied): Applied => ({
			scope,
			project,
		});
		return {
			applied: {
				ready: applied(ready),
				mine: applied(mine),
				projects: applied(projects),
			},
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
