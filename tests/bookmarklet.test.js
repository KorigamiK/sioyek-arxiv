import { expect, test } from "bun:test";

const result = Bun.spawnSync(["python3", "sioyek_open.py", "bookmarklet", "--print"]);
if (result.exitCode !== 0) throw new Error(result.stderr.toString());
const script = result.stdout.toString().trim().replace(/^javascript:/, "");

function run({ url, tags = {}, title = "A page", pdf, canonical, jsonLd = [], paste = null, contentType = "text/html" }) {
  const location = { href: url };
  const alerts = [];
  const document = {
    title, contentType,
    querySelectorAll(selector) {
      if (selector.startsWith("meta")) return Object.entries(tags).flatMap(([key, values]) =>
        values.map(value => ({ getAttribute: name => ({ name: key, content: value })[name] ?? null })));
      return jsonLd.map(value => ({ textContent: typeof value === "string" ? value : JSON.stringify(value) }));
    },
    querySelector(selector) {
      const href = selector.includes("canonical") ? canonical : pdf;
      return href ? { href } : null;
    },
  };
  new Function("location", "document", "prompt", "alert", script)(location, document, () => paste, value => alerts.push(value));
  const target = new URL(location.href);
  return { target, alerts, metadata: target.searchParams.has("metadata") ? JSON.parse(target.searchParams.get("metadata")) : null };
}

test("article page sends its declared PDF and citation metadata", () => {
  const tags = { citation_title: ["A & B: 音楽"], citation_author: ["Doe, Jane", "John Roe"],
    citation_pdf_url: ["/download/paper.pdf?key=1&name=two"], citation_doi: ["10.1234/music"] };
  const jsonLd = [{ "@type": "ScholarlyArticle", abstract: "An abstract" }];
  const result = run({ url: "https://journal.example/article/1", title: "Paper | Journal", tags, jsonLd,
    canonical: "https://journal.example/article/1" });
  expect(result.target.protocol).toBe("sioyek-open:");
  expect(result.target.searchParams.get("url")).toBe("https://journal.example/download/paper.pdf?key=1&name=two");
  expect(result.metadata.meta).toEqual(tags);
  expect(result.metadata.json_ld).toEqual(jsonLd);
  expect(result.metadata.page_url).toBe("https://journal.example/article/1");
});

test("pasting another PDF does not attach the unrelated current page", () => {
  const result = run({ url: "https://unrelated.example/", tags: { "og:title": ["Wrong article"] },
    paste: "https://www.music-ir.org/mirex/abstracts/2019/SDE1.pdf" });
  expect(result.metadata).toEqual({});
  expect(result.target.searchParams.get("url")).toContain("SDE1.pdf");
});

test("arXiv page captures authors while retaining the abstract URL", () => {
  const result = run({ url: "https://arxiv.org/abs/1706.03762", tags: { citation_author: ["Jane Doe"] } });
  expect(result.target.searchParams.get("url")).toBe("https://arxiv.org/abs/1706.03762");
  expect(result.metadata.meta.citation_author).toEqual(["Jane Doe"]);
});

test("PDF viewer and local PDF are supported without page citation tags", () => {
  for (const url of ["https://example.org/download?id=1", "file:///Users/test/Paper.pdf"]) {
    const result = run({ url, contentType: "application/pdf", title: "Paper.pdf" });
    expect(result.target.searchParams.get("url")).toBe(url);
    expect(result.metadata.page_title).toBe("Paper.pdf");
  }
});

test("large metadata is bounded, retains useful citations, and ignores malformed JSON-LD", () => {
  const result = run({ url: "https://example.org/paper.pdf", tags: { citation_title: ["Useful title"],
    "og:description": ["界".repeat(2000), "界".repeat(2000)] },
    jsonLd: ["{broken", { "@type": "ScholarlyArticle", description: "界".repeat(4000) }] });
  expect(encodeURIComponent(JSON.stringify(result.metadata)).length).toBeLessThanOrEqual(24000);
  expect(result.metadata.meta.citation_title).toEqual(["Useful title"]);
});

test("PDF links with unsupported protocols are rejected", () => {
  const result = run({ url: "https://journal.example/article", pdf: "javascript:alert(1)" });
  expect(result.alerts).toHaveLength(1);
  expect(result.target.protocol).toBe("https:");
});
