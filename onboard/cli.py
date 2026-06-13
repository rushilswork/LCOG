"""CLI entry point for the Legacy Codebase Onboarding Generator.

Usage:
    onboard analyze <repo_path> [OPTIONS]

Options:
    --output DIR         Output directory for the MkDocs site  [default: <repo>/onboarding-guide]
    --provider TEXT      LLM provider: groq or gemini          [default: groq]
    --api-key TEXT       API key (or set GROQ_API_KEY / GEMINI_API_KEY env vars)
    --model TEXT         Override the default model for the chosen provider
    --model-tier TEXT    Speed/quality tier: fast|balanced|best [default: fast]
    --site-name TEXT     Title for the generated site
    --max-modules INT    Cap on modules sent to the LLM        [default: 50]
    --skip-llm           Generate stub narratives without calling any LLM
    --serve              Run `mkdocs serve` after skeleton build (no wait for stage 4)
    --workers INT        Parallel workers for file parsing (0=auto)
    --retry-failed       Re-generate only modules with failed narratives
    --no-cache           Disable parse cache; re-parse all files from scratch
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time as _time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

console = Console()

PROVIDER_ENV_VARS = {
    "groq":   "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# Speed / quality tiers per provider
MODEL_TIERS: dict[str, dict[str, str]] = {
    "groq": {
        "fast":     "llama-3.1-8b-instant",    # ~10× higher RPM, ideal for large repos
        "balanced": "llama-3.3-70b-versatile",
        "best":     "llama-3.3-70b-versatile",
    },
    "gemini": {
        "fast":     "gemini-2.0-flash",
        "balanced": "gemini-2.0-flash",
        "best":     "gemini-1.5-pro",
    },
}


def _banner():
    console.print(
        "\n[bold blue]Legacy Codebase Onboarding Generator[/bold blue]\n",
        highlight=False,
    )


def _step(label: str):
    console.rule(f"[bold]{label}[/bold]")


def _fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _build_stub_guide(graph, history, site_name: str, reading_order: list, max_modules: int):
    """Build an OnboardingGuide with stub narratives for all modules (no LLM)."""
    from onboard.stages.narrative_gen import OnboardingGuide, ModuleNarrative

    guide = OnboardingGuide(
        system_overview=(
            f"# {site_name}\n\n"
            "> **Generating narratives...** Refresh this page in a moment "
            "while the AI writes module descriptions in the background.\n"
        ),
        reading_order=reading_order,
        major_themes=history.major_themes,
    )
    for path in reading_order[:max_modules]:
        node_data = graph.nodes.get(path, {})
        lang = node_data.get("language", "?")
        stem = Path(path).stem
        if stem == "__init__" and Path(path).parent != Path("."):
            mod_title = Path(path).parent.name.replace("_", " ").title() + " (init)"
        else:
            mod_title = stem.replace("_", " ").title()

        guide.modules[path] = ModuleNarrative(
            path=path,
            title=mod_title,
            summary=f"*Generating narrative for `{path}` ({lang})...*",
            walkthrough="",
            design_notes="",
            pitfalls="",
            patterns="",
            architecture_notes="",
            entry_points_usage="",
            code_walkthrough="",
            sequence_diagram="",
            state_machine_diagram="",
            data_flow_snippet="",
        )
    guide.guided_tour = (
        "# Guided Tour\n\n"
        "> *Generating — refresh shortly.*\n"
    )
    return guide


def _start_serve(output_dir: Path) -> Optional[subprocess.Popen]:
    """Start mkdocs serve and open the browser. Returns the Popen handle or None."""
    try:
        proc = subprocess.Popen(["mkdocs", "serve"], cwd=output_dir)

        def _launch_browser():
            _time.sleep(2)
            webbrowser.open("http://127.0.0.1:8000")

        threading.Thread(target=_launch_browser, daemon=True).start()
        console.print("  [dim]Opening browser at http://127.0.0.1:8000 ...[/dim]")
        return proc
    except FileNotFoundError:
        console.print(
            "[red]Error:[/red] mkdocs not found. "
            "Run: pip install mkdocs-material"
        )
        return None
    except OSError as exc:
        console.print(f"[red]Error:[/red] Could not start mkdocs serve: {exc}")
        return None


def _find_failed_modules(output_dir: Path) -> list[str]:
    """Scan existing module pages for failed narrative admonitions."""
    failed: list[str] = []
    mod_dir = output_dir / "docs" / "modules"
    if not mod_dir.exists():
        return failed
    for md_file in mod_dir.glob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "Narrative unavailable" in content or "Generation failed" in content:
            m = re.search(r'\*\*File:\*\* `([^`]+)`', content)
            if m:
                failed.append(m.group(1))
    return failed


@click.group()
def main():
    """Generate a structured onboarding guide for a legacy codebase."""
    pass


@main.command()
@click.argument("repo_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "-o", default=None,
              type=click.Path(path_type=Path),
              help="Output directory [default: <repo>/onboarding-guide]")
@click.option("--provider", default="groq", show_default=True,
              type=click.Choice(["groq", "gemini"], case_sensitive=False),
              help="LLM provider to use")
@click.option("--api-key", default=None,
              help="API key. Falls back to GROQ_API_KEY or GEMINI_API_KEY env var.")
@click.option("--model", default=None,
              help="Override the model (takes precedence over --model-tier)")
@click.option("--model-tier", default="fast", show_default=True,
              type=click.Choice(["fast", "balanced", "best"], case_sensitive=False),
              help="Speed/quality tier. fast=8b-instant/gemini-2.0-flash, best=70b/gemini-1.5-pro")
@click.option("--site-name", default=None,
              help="Site title [default: <repo-name> Onboarding]")
@click.option("--max-modules", default=50, show_default=True, type=int,
              help="Maximum modules to send to the LLM")
@click.option("--skip-llm", is_flag=True, default=False,
              help="Skip LLM stage; produce stub narratives only")
@click.option("--serve", is_flag=True, default=False,
              help="Start mkdocs serve immediately after skeleton build (browser opens right away)")
@click.option("--workers", default=0, show_default=True, type=int,
              help="Parallel workers for file parsing (0=auto, uses CPU count)")
@click.option("--retry-failed", is_flag=True, default=False,
              help="Re-generate only modules whose narrative previously failed")
@click.option("--no-cache", is_flag=True, default=False,
              help="Disable parse cache; re-parse all files from scratch")
def analyze(
    repo_path: Path,
    output: Path,
    provider: str,
    api_key: str,
    model: str,
    model_tier: str,
    site_name: str,
    max_modules: int,
    skip_llm: bool,
    serve: bool,
    workers: int,
    retry_failed: bool,
    no_cache: bool,
):
    """Analyse REPO_PATH and generate an onboarding guide."""
    _banner()

    provider = provider.lower()

    # Resolve API key from env if not passed directly
    if not api_key and not skip_llm:
        env_var = PROVIDER_ENV_VARS.get(provider, "")
        api_key = os.environ.get(env_var, "")

    if not skip_llm and not api_key:
        env_var = PROVIDER_ENV_VARS.get(provider, "API_KEY")
        console.print(
            f"[red]Error:[/red] No API key for provider '{provider}'. "
            f"Set {env_var} or pass --api-key.\n"
            "Use --skip-llm to generate stub narratives without an API key."
        )
        sys.exit(1)

    # Resolve model: explicit --model > tier lookup > provider default
    if not model:
        model = MODEL_TIERS.get(provider, {}).get(model_tier, "")

    site_name = site_name or f"{repo_path.name} Onboarding"
    output = (output or repo_path / "onboarding-guide").resolve()
    cache_dir = None if no_cache else (repo_path / ".onboard_cache")

    # -----------------------------------------------------------------------
    # Stages 1-3: run in parallel (all are independent of each other)
    # -----------------------------------------------------------------------
    _step("Stages 1-3 -- Analysis (parallel)")

    from onboard.stages.static_analysis import analyze_repo, summarize_graph
    from onboard.stages.git_analysis import analyze_git
    from onboard.stages.doc_collector import collect_docs

    _parallel_start = _time.monotonic()

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        t1 = prog.add_task("[cyan]Static analysis[/cyan]   ", total=None)
        t2 = prog.add_task("[cyan]Git history[/cyan]        ", total=None)
        t3 = prog.add_task("[cyan]Doc collection[/cyan]     ", total=None)

        def _mark_done(fut, task_id: int, label: str) -> None:
            elapsed = _time.monotonic() - _parallel_start
            t_str = _fmt_elapsed(elapsed)
            prog.stop_task(task_id)
            try:
                failed = fut.exception() is not None
            except Exception:
                failed = False
            if failed:
                prog.update(task_id,
                    description=f"[red]X {label}[/red]  [dim]({t_str})[/dim]")
            else:
                prog.update(task_id,
                    description=f"[green]done {label}[/green]  [dim]({t_str})[/dim]")

        _stage_exec = ThreadPoolExecutor(max_workers=3)
        try:
            f1 = _stage_exec.submit(analyze_repo, repo_path, workers=workers,
                                    cache_dir=cache_dir)
            f2 = _stage_exec.submit(analyze_git, repo_path)
            f3 = _stage_exec.submit(collect_docs, repo_path)

            f1.add_done_callback(lambda f: _mark_done(f, t1, "Static analysis"))
            f2.add_done_callback(lambda f: _mark_done(f, t2, "Git history    "))
            f3.add_done_callback(lambda f: _mark_done(f, t3, "Doc collection "))

            try:
                graph = f1.result()
            except Exception as exc:
                console.print(f"\n[red]Error:[/red] Static analysis failed: {exc}")
                _stage_exec.shutdown(wait=False, cancel_futures=True)
                sys.exit(1)
            try:
                history = f2.result()
            except Exception as exc:
                console.print(f"\n[red]Error:[/red] Git analysis failed: {exc}")
                _stage_exec.shutdown(wait=False, cancel_futures=True)
                sys.exit(1)
            try:
                corpus = f3.result()
            except Exception as exc:
                console.print(f"\n[red]Error:[/red] Doc collection failed: {exc}")
                _stage_exec.shutdown(wait=False, cancel_futures=True)
                sys.exit(1)
            _stage_exec.shutdown(wait=True)
        except KeyboardInterrupt:
            _stage_exec.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception:
            _stage_exec.shutdown(wait=False, cancel_futures=True)
            raise

    _parallel_total = _time.monotonic() - _parallel_start
    console.print(f"  [dim]Parallel wall time: {_fmt_elapsed(_parallel_total)}[/dim]")

    summary = summarize_graph(graph)
    console.print(
        f"  [dim]Static:[/dim]  "
        f"Files: [cyan]{summary['total_files']}[/cyan]  "
        f"Edges: [cyan]{summary['total_edges']}[/cyan]  "
        f"Entry points: [cyan]{len(summary['entry_points'])}[/cyan]  "
        f"Circular deps: [cyan]{summary['circular_deps']}[/cyan]"
    )
    console.print(f"  [dim]         [/dim]  Languages: {summary['languages']}")
    console.print(
        f"  [dim]Git:    [/dim]  "
        f"Commits: [cyan]{history.total_commits}[/cyan]  "
        f"Files tracked: [cyan]{len(history.files)}[/cyan]"
    )
    if history.major_themes:
        console.print(f"  [dim]         [/dim]  Themes: {', '.join(history.major_themes[:8])}")
    console.print(
        f"  [dim]Docs:   [/dim]  "
        f"Fragments: [cyan]{len(corpus.fragments)}[/cyan]  "
        f"READMEs: [cyan]{len(corpus.readmes())}[/cyan]  "
        f"Arch docs: [cyan]{len(corpus.arch_docs())}[/cyan]"
    )

    # -----------------------------------------------------------------------
    # Build skeleton site immediately — user can open it now
    # -----------------------------------------------------------------------
    _step("Skeleton site (instant preview)")
    from onboard.output.mkdocs_builder import build_site, update_module_page

    import networkx as nx
    try:
        _ro = list(nx.topological_sort(graph))
    except nx.NetworkXUnfeasible:
        _cond = nx.condensation(graph)
        _ro = []
        for _scc in list(nx.topological_sort(_cond)):
            _ro.extend(list(_cond.nodes[_scc]["members"]))

    stub_guide = _build_stub_guide(graph, history, site_name, _ro, max_modules)
    build_site(guide=stub_guide, graph=graph, output_dir=output, site_name=site_name)
    console.print(f"  [green]Skeleton ready[/green] → {output}")
    if cache_dir:
        console.print(f"  [dim]Parse cache: {cache_dir}[/dim]")

    # Start mkdocs serve NOW (don't wait for stage 4)
    _serve_proc: Optional[subprocess.Popen] = None
    if serve:
        _serve_proc = _start_serve(output)

    try:
        # -----------------------------------------------------------------------
        # Stage 4: Narrative generation
        # -----------------------------------------------------------------------
        _step("Stage 4 -- LLM narrative generation")
        from onboard.stages.narrative_gen import generate_guide

        # Handle --retry-failed
        only_paths: Optional[list[str]] = None
        if retry_failed:
            only_paths = _find_failed_modules(output)
            if not only_paths:
                console.print("  [yellow]No failed narratives found — nothing to retry.[/yellow]")
            else:
                console.print(
                    f"  Retrying [cyan]{len(only_paths)}[/cyan] failed module(s): "
                    + ", ".join(only_paths[:5])
                    + ("..." if len(only_paths) > 5 else "")
                )

        # When retry-failed found nothing to retry, skip stage 4 entirely.
        # (only_paths=[] is falsy — generate_guide would silently run a full generation)
        _nothing_to_retry = retry_failed and only_paths is not None and len(only_paths) == 0

        if skip_llm or _nothing_to_retry:
            if skip_llm:
                console.print("  [yellow]--skip-llm set; using stub narratives.[/yellow]")
            guide = stub_guide
        else:
            console.print(
                f"  Provider: [cyan]{provider}[/cyan]  "
                f"Model: [cyan]{model}[/cyan]  "
                f"Tier: [cyan]{model_tier}[/cyan]"
            )

            # Progressive callback: write each module page as soon as it's done
            # mkdocs serve picks up the change via its file-watcher automatically
            def _on_module_done(path: str, narrative) -> None:
                try:
                    update_module_page(narrative, graph, output / "docs" / "modules")
                except Exception:
                    pass

            guide = generate_guide(
                graph=graph,
                history=history,
                corpus=corpus,
                repo_path=repo_path,
                provider=provider,
                api_key=api_key,
                model=model,
                max_modules=max_modules,
                console=console,
                module_done_callback=_on_module_done,
                only_paths=only_paths,
                cache_dir=cache_dir,
            )

        console.print(f"  Modules narrated: [cyan]{len(guide.modules)}[/cyan]")

        # -----------------------------------------------------------------------
        # Final site rebuild (updates index, guided tour, arc42, etc.)
        # -----------------------------------------------------------------------
        _step("Final site rebuild")
        build_site(guide=guide, graph=graph, output_dir=output, site_name=site_name)
        console.print(f"\n[green]Done![/green] Guide written to [bold]{output}[/bold]")
        console.print(f"   [dim]cd {output} && mkdocs serve[/dim]\n")

        if _serve_proc is not None:
            try:
                _serve_proc.wait()
            except KeyboardInterrupt:
                _serve_proc.terminate()
        elif serve:
            # --serve was set but _start_serve failed (mkdocs not found) — already warned
            pass

    except KeyboardInterrupt:
        # Ctrl+C during stage 4 or final rebuild — kill the serve process immediately
        if _serve_proc is not None:
            _serve_proc.terminate()
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(1)
    except Exception:
        # Unexpected error — still kill the serve process so it doesn't linger
        if _serve_proc is not None:
            _serve_proc.terminate()
        raise


@main.command()
@click.argument("guide_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                default="onboarding-guide")
def serve(guide_dir: Path):
    """Serve an existing onboarding guide with MkDocs."""
    console.print(f"Serving [bold]{guide_dir}[/bold]...")
    proc = _start_serve(guide_dir)
    if proc:
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()


if __name__ == "__main__":
    main()
