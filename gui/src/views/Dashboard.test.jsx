import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { CliError } from "@/lib/api";
import { ContractError } from "@/lib/parser";
import { StatusFailure } from "./Dashboard.jsx";

vi.mock("@/components/FadeIn", () => ({
  FadeIn: ({ children }) => <div>{children}</div>,
}));

const t = (key) => key;

describe("Dashboard status failure diagnostics", () => {
  it("分别展示 timeout、exit code、stdout 与 stderr", () => {
    const error = new CliError({
      stdout: "partial doctor report",
      stderr: "runner timed out",
      exit_code: -1,
      timed_out: true,
    });

    const html = renderToStaticMarkup(<StatusFailure error={error} t={t} />);

    expect(html).toContain("dash.diagnosticTimeout");
    expect(html).toContain("dash.diagnosticExitCode");
    expect(html).toContain("partial doctor report");
    expect(html).toContain("runner timed out");
  });

  it("契约超时错误同样带出结构化输出", () => {
    const error = new ContractError("CLI 执行超时", {
      stdout: '{"schema":"zcode-keysmith/v1"',
      stderr: "pipe still open",
      exit_code: 0,
      timed_out: true,
    });

    const html = renderToStaticMarkup(<StatusFailure error={error} t={t} />);

    expect(html).toContain("dash.diagnosticTimeout");
    expect(html).toContain("pipe still open");
    expect(html).toContain("{&quot;schema&quot;:&quot;zcode-keysmith/v1&quot;");
  });

  it("普通错误没有结构化输出时展示 message", () => {
    const html = renderToStaticMarkup(
      <StatusFailure error={new Error("status bridge unavailable")} t={t} />,
    );

    expect(html).toContain("status bridge unavailable");
  });
});
