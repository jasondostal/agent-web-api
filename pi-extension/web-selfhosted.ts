/**
 * web-selfhosted — web_search + web_fetch for pi, backed by a self-hosted
 * SearXNG (metasearch) + trafilatura-api (URL → markdown) stack behind a
 * LAN-only reverse proxy.
 *
 * No cloud search APIs, no quotas. If pi-web-access is installed, disable its
 * own web_search via ~/.pi/web-search.json {"webSearch":{"enabled":false}} so
 * this tool owns the name; its fetch_content remains as fallback for
 * GitHub-clone / video / JS-heavy pages that a plain HTTP fetch can't render.
 *
 * Endpoint resolution: SELFHOSTED_WEB_BASE env var, else "base" in
 * ~/.pi/web-selfhosted.json. Install: symlink into ~/.pi/agent/extensions/.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

function resolveBase(): string | null {
	if (process.env.SELFHOSTED_WEB_BASE) return process.env.SELFHOSTED_WEB_BASE;
	try {
		const cfg = JSON.parse(readFileSync(join(homedir(), ".pi", "web-selfhosted.json"), "utf8"));
		if (typeof cfg.base === "string" && cfg.base) return cfg.base;
	} catch {
		/* no config file */
	}
	return null;
}

const BASE = resolveBase();
const NO_BASE_MSG =
	'web-selfhosted: no endpoint configured. Set SELFHOSTED_WEB_BASE or put {"base": "https://..."} in ~/.pi/web-selfhosted.json';

async function getJson(url: string, signal?: AbortSignal): Promise<any> {
	const res = await fetch(url, { signal, headers: { "User-Agent": "pi-web-selfhosted/0.1" } });
	const text = await res.text();
	let data: any = null;
	try {
		data = JSON.parse(text);
	} catch {
		/* non-JSON error body */
	}
	if (!res.ok) {
		const detail = data?.detail ?? text.slice(0, 300);
		throw new Error(`HTTP ${res.status}: ${detail}`);
	}
	return data;
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "web_search",
		label: "Web Search (self-hosted)",
		description:
			"Search the web via the self-hosted SearXNG instance (aggregates Google/Bing/DDG/Brave). " +
			"Returns ranked results with title, URL, and snippet. Follow up with web_fetch on promising URLs " +
			"to read full page content. LAN-hosted: no API quotas, use freely.",
		promptSnippet: "Use web_search for web research, then web_fetch to read promising results in full.",
		parameters: Type.Object({
			query: Type.String({ description: "Search query" }),
			numResults: Type.Optional(Type.Number({ description: "Max results to return (default 8, max 20)" })),
			timeRange: Type.Optional(
				Type.Union([Type.Literal("day"), Type.Literal("week"), Type.Literal("month"), Type.Literal("year")], {
					description: "Restrict results by recency",
				}),
			),
		}),
		async execute(_callId, params, signal) {
			if (!BASE) {
				return { content: [{ type: "text", text: NO_BASE_MSG }], details: { error: "unconfigured" }, isError: true };
			}
			try {
				const n = Math.min(params.numResults ?? 8, 20);
				const qs = new URLSearchParams({ q: params.query, format: "json" });
				if (params.timeRange) qs.set("time_range", params.timeRange);
				const data = await getJson(`${BASE}/search?${qs}`, signal);
				const results = (data.results ?? []).slice(0, n);
				if (results.length === 0) {
					return {
						content: [{ type: "text", text: `No results for: ${params.query}` }],
						details: { query: params.query, count: 0 },
					};
				}
				const lines = results.map(
					(r: any, i: number) =>
						`${i + 1}. ${r.title}\n   ${r.url}\n   ${(r.content ?? "").replace(/\s+/g, " ").trim()}`,
				);
				return {
					content: [{ type: "text", text: lines.join("\n\n") }],
					details: { query: params.query, count: results.length },
				};
			} catch (e: any) {
				return {
					content: [{ type: "text", text: `web_search failed: ${e.message}` }],
					details: { error: e.message },
					isError: true,
				};
			}
		},
	});

	pi.registerTool({
		name: "web_fetch",
		label: "Web Fetch (self-hosted)",
		description:
			"Fetch a URL and extract its main content as clean markdown via the self-hosted trafilatura service. " +
			"Best for articles, docs, blogs, READMEs. Cannot execute JavaScript — for SPA-heavy pages or GitHub " +
			"repo deep-dives, fall back to fetch_content. LAN-hosted: no API quotas, use freely.",
		promptSnippet: "Use web_fetch to read a page as markdown after finding it with web_search.",
		parameters: Type.Object({
			url: Type.String({ description: "URL to fetch and extract" }),
		}),
		async execute(_callId, params, signal) {
			if (!BASE) {
				return { content: [{ type: "text", text: NO_BASE_MSG }], details: { error: "unconfigured" }, isError: true };
			}
			try {
				const qs = new URLSearchParams({ url: params.url });
				const data = await getJson(`${BASE}/fetch?${qs}`, signal);
				const header = [
					data.title ? `# ${data.title}` : null,
					data.date ? `Date: ${data.date}` : null,
					data.sitename ? `Site: ${data.sitename}` : null,
					data.truncated ? `(content truncated)` : null,
				]
					.filter(Boolean)
					.join("\n");
				return {
					content: [{ type: "text", text: `${header}\n\n${data.content}` }],
					details: { url: params.url, title: data.title, truncated: data.truncated },
				};
			} catch (e: any) {
				return {
					content: [{ type: "text", text: `web_fetch failed: ${e.message}` }],
					details: { error: e.message },
					isError: true,
				};
			}
		},
	});
}
