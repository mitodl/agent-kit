import { describe, expect, it } from "vitest";
import {
	assigneeOptions,
	filterByAssignee,
	matchesAssignee,
	normalizeAssignee,
	UNASSIGNED,
} from "./assignee.js";

const ME = "dfrapp@mit.edu";

describe("normalizeAssignee", () => {
	it("strips the session qualifier a claim writes", () => {
		expect(normalizeAssignee(`${ME}#003625bb`)).toBe(ME);
		expect(normalizeAssignee(`${ME}#1ccd2dfd`)).toBe(ME);
		expect(normalizeAssignee(ME)).toBe(ME);
	});

	it("strips only a trailing session qualifier, as the server does", () => {
		// An identity that contains a '#' is not a qualified one.
		expect(normalizeAssignee("team#blue@example.com")).toBe(
			"team#blue@example.com",
		);
		expect(normalizeAssignee("team#blue@example.com#1ccd2dfd")).toBe(
			"team#blue@example.com",
		);
		expect(normalizeAssignee("Tobias Macey#5e313f6d")).toBe("Tobias Macey");
	});

	it("reads nobody as null", () => {
		expect(normalizeAssignee(null)).toBeNull();
		expect(normalizeAssignee(undefined)).toBeNull();
		expect(normalizeAssignee("")).toBeNull();
		// A qualifier with no owner names nobody either.
		expect(normalizeAssignee("#abc")).toBeNull();
	});
});

describe("matchesAssignee", () => {
	it("passes everything when nothing is selected", () => {
		expect(matchesAssignee({ assignee: null }, [])).toBe(true);
		expect(matchesAssignee({ assignee: ME }, [])).toBe(true);
	});

	it("matches every session of one person", () => {
		expect(matchesAssignee({ assignee: `${ME}#003625bb` }, [ME])).toBe(true);
		expect(matchesAssignee({ assignee: ME }, [ME])).toBe(true);
		expect(matchesAssignee({ assignee: "other@mit.edu" }, [ME])).toBe(false);
	});

	it("matches a null assignee only when Unassigned is selected", () => {
		expect(matchesAssignee({ assignee: null }, [UNASSIGNED])).toBe(true);
		expect(matchesAssignee({ assignee: null }, [ME])).toBe(false);
		expect(matchesAssignee({ assignee: ME }, [UNASSIGNED])).toBe(false);
	});

	it("is the union of Unassigned and the people selected", () => {
		const selected = [UNASSIGNED, ME];

		expect(matchesAssignee({ assignee: null }, selected)).toBe(true);
		expect(matchesAssignee({ assignee: `${ME}#1` }, selected)).toBe(true);
		expect(matchesAssignee({ assignee: "other@mit.edu" }, selected)).toBe(
			false,
		);
	});
});

describe("filterByAssignee", () => {
	const rows = [
		{ slug: "a", assignee: `${ME}#1` },
		{ slug: "b", assignee: ME },
		{ slug: "c", assignee: null },
		{ slug: "d", assignee: "other@mit.edu" },
	];

	it("returns the same array for an empty selection", () => {
		expect(filterByAssignee(rows, [])).toBe(rows);
	});

	it("keeps exactly the matching rows", () => {
		expect(filterByAssignee(rows, [ME]).map((row) => row.slug)).toEqual([
			"a",
			"b",
		]);
		expect(filterByAssignee(rows, [UNASSIGNED]).map((r) => r.slug)).toEqual([
			"c",
		]);
	});
});

describe("assigneeOptions", () => {
	it("lists one entry per person, sorted", () => {
		const rows = [
			{ assignee: `${ME}#003625bb` },
			{ assignee: `${ME}#1ccd2dfd` },
			{ assignee: ME },
			{ assignee: "ada@mit.edu" },
			{ assignee: null },
		];

		expect(assigneeOptions(rows, [])).toEqual(["ada@mit.edu", ME]);
	});

	it("keeps a selected person that no loaded task carries", () => {
		expect(assigneeOptions([], ["gone@mit.edu", UNASSIGNED])).toEqual([
			"gone@mit.edu",
		]);
	});
});
