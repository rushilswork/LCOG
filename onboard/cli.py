"""CLI entry point for the Legacy Codebase Onboarding Generator.

Usage:
    onboard analyze <repo_path> [OPTIONS]

Options:
    --output DIR         Output directory for the MkDocs site  [default: <repo>/onboarding-guide]
    --provider TEXT      LLM provider: groq or gemini          [default: groq]
    --api-key TEXT       API key (or set GROQ_API_KEY / GEMINI_API_KEY env vars)
    --model TEXT         Override the default model for the chosen provider
    --site-name TEXT     Title for the generated site
    --max-modules INT    Cap on modules sent to the LLM        [default: 50]
    --skip-llm           Generate stub narratives without calling any LLM
    --serve              Run `mkdocs serve` after generation
    --workers INT        Parallel workers for file parsing (0=auto)
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

console = Console()

PROVIDER_ENV_VARS = {
    "groq":   "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _banner():
    console.print(
        "\n[bold blue]Legacy Codebase Onboarding Generator[/bold blue]\n",
        highlight=False,
    )


def _step(label: str):
    console.rule(f"[bold]{label}[/bold]")


@click.group()
def main():
    """Generate a structured onboarding guide for a legacy codebase."""
    pass


@main.command()
@click.argument("repo_path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", "-o", default=None,
              type=click.Path(path_type=Path), help="Output directory [default: <repo>/onboarding-guide]")
@click.option("--provider", default="groq", show_default=True,
              type=click.Choice(["groq", "gemini"], case_sensitive=False),
              help="LLM provider to use")
@click.option("--api-key", default=None,
              help="API key. Falls back to GROQ_API_KEY or GEMINI_API_KEY env var.")
@click.option("--model", default=None,
              help="Override the default model for the chosen provider")
@click.option("--site-name", default=None,
              help="Site title [default: <repo-name> Onboarding]")
@click.option("--max-modules", default=50, show_default=True, type=int,
              help="Maximum modules to send to the LLM")
@click.option("--skip-llm", is_flag=True, default=False,
              help="Skip LLM stage; produce stub narratives only")
@click.option("--serve", is_flag=True, default=False,
              help="Run `mkdocs serve` after generation")
@click.option("--workers", default=0, show_default=True, type=int,
              help="Parallel workers for file parsing (0=auto, uses CPU count)")
def analyze(
    repo_path: Path,
    output: Path,
    provider: str,
    api_key: str,
    model: str,
    site_name: str,
    max_modules: int,
    skip_llm: bool,
    serve: bool,
    workers: int,
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

    site_name = site_name or f"{repo_path.name} Onboarding"
    output = (output or repo_path / "onboarding-guide").resolve()

    # -----------------------------------------------------------------------
    # Stages 1–3: run in parallel (all are independent of each other)
    # -----------------------------------------------------------------------
    _step("Stages 1-3 -- Analysis (parallel)")

    from onboard.stages.static_analysis import analyze_repo, summarize_graph
    from onboard.stages.git_analysis import analyze_git
    from onboard.stages.doc_collector import collect_docs

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        t1 = prog.add_task("[cyan]Static analysis[/cyan]   ", total=None)
        t2 = prog.add_task("[cyan]Git history[/cyan]        ", total=None)
        t3 = prog.add_task("[cyan]Doc collection[/cyan]     ", total=None)

        with ThreadPoolExecutor(max_workers=3) as executor:
            f1 = executor.submit(analyze_repo, repo_path, workers=workers)
            f2 = executor.submit(analyze_git, repo_path)
            f3 = executor.submit(collect_docs, repo_path)

            graph   = f1.result(); prog.update(t1, completed=True)
            history = f2.result(); prog.update(t2, completed=True)
            corpus  = f3.result(); prog.update(t3, completed=True)

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
    # Stage 4: Narrative generation
    # -----------------------------------------------------------------------
    _step("Stage 4 -- LLM narrative generation")
    from onboard.stages.narrative_gen import generate_guide

    if skip_llm:
        console.print("  [yellow]--skip-llm set; generating stub narratives.[/yellow]")
        from onboard.stages.narrative_gen import OnboardingGuide, ModuleNarrative
        import networkx as nx

        try:
            reading_order = list(nx.topological_sort(graph))
        except nx.NetworkXUnfeasible:
            cond = nx.condensation(graph)
            topo_sccs = list(nx.topological_sort(cond))
            reading_order = []
            for scc_node in topo_sccs:
                reading_order.extend(list(cond.nodes[scc_node]["members"]))
        guide = OnboardingGuide(
            system_overview=f"# {site_name}\n\nStub overview -- run without --skip-llm for full narrative.",
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
                summary=f"*Stub -- {lang} module at `{path}`.*",
                walkthrough="",
                design_notes="",
                pitfalls="",
            )
        guide.guided_tour = "# Guided Tour\n\n*Run without --skip-llm to generate a full tour.*\n"
    else:
        from onboard.stages.narrative_gen import PROVIDER_DEFAULTS
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        resolved_model = model or defaults.get("model", "")
        console.print(f"  Provider: [cyan]{provider}[/cyan]  Model: [cyan]{resolved_model}[/cyan]")

        guide = generate_guide(
            graph=graph,
            history=history,
            corpus=corpus,
            repo_path=repo_path,
            provider=provider,
            api_key=api_key,
            model=resolved_model,
            max_modules=max_modules,
            console=console,
        )

    console.print(f"  Modules narrated: [cyan]{len(guide.modules)}[/cyan]")

    # -----------------------------------------------------------------------
    # Output: MkDocs site
    # -----------------------------------------------------------------------
    _step("Building MkDocs site")
    from onboard.output.mkdocs_builder import build_site

    build_site(guide=guide, graph=graph, output_dir=output, site_name=site_name)
    console.print(f"\n[green]Done![/green] Guide written to [bold]{output}[/bold]")
    console.print(f"   [dim]cd {output} && mkdocs serve[/dim]\n")

    if serve:
        try:
            subprocess.run(["mkdocs", "serve"], cwd=output, check=False)
        except FileNotFoundError:
            console.print(
                "[red]Error:[/red] mkdocs not found. "
                "Run: pip install mkdocs-material"
            )


@main.command()
@click.argument("guide_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
                default="onboarding-guide")
def serve(guide_dir: Path):
    """Serve an existing onboarding guide with MkDocs."""
    console.print(f"Serving [bold]{guide_dir}[/bold]...")
    try:
        subprocess.run(["mkdocs", "serve"], cwd=guide_dir, check=False)
    except FileNotFoundError:
        console.print(
            "[red]Error:[/red] mkdocs not found. "
            "Run: pip install mkdocs-material"
        )


if __name__ == "__main__":
    main()
