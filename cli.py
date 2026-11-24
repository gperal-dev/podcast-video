"""PodcastForge Studio CLI — Full command parity with Web UI."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from backend.config import get_settings
from backend.core.diagnostics import run_full_doctor
from backend.core.pipeline import JobManager
from backend.core.profile_store import ProfileStore
from backend.core.watcher import FolderWatcher


app = typer.Typer(
    name="podcastforge",
    help="PodcastForge Studio CLI — Convierte podcasts en Shorts, vídeos medios y largos.",
    no_args_is_help=True,
)
profiles_app = typer.Typer(help="Gestión de perfiles de podcaster.")
app.add_typer(profiles_app, name="profiles")

console = Console()


@app.command("doctor")
def doctor(
    skip_api: bool = typer.Option(False, "--skip-api", help="Omite pruebas en vivo contra OpenRouter/Pexels/Pixabay."),
) -> None:
    """Comprueba FFmpeg, Whisper, aceleración HW, fuentes y claves .env."""
    settings = get_settings()
    console.print("[bold cyan]PodcastForge Studio — Diagnóstico del sistema[/bold cyan]\n")
    report = run_full_doctor(settings, test_apis=not skip_api)

    table = Table(title="Componentes Locales", show_header=True, header_style="bold magenta")
    table.add_column("Componente", style="cyan")
    table.add_column("Estado")
    table.add_column("Detalle")

    table.add_row("Python", "[green]OK[/green]", report["python_version"])

    ff = report["ffmpeg"]
    ff_ok = ff["ffmpeg_ok"] and ff["ffprobe_ok"]
    table.add_row(
        "FFmpeg & FFprobe",
        "[green]OK[/green]" if ff_ok else "[red]FALTA[/red]",
        ff["version"] or "No encontrado en PATH ni WinGet",
    )

    missing_filters = [k for k, v in ff["required_filters"].items() if not v]
    table.add_row(
        "Filtros FFmpeg",
        "[green]OK[/green]" if not missing_filters else "[red]INCOMPLETO[/red]",
        "ass, acrossfade, loudnorm, silencedetect, zoompan"
        if not missing_filters
        else f"Faltan: {', '.join(missing_filters)}",
    )

    enc_summary = ", ".join(
        f"{k}:{'OK' if v.get('ok') else 'NO'}"
        for k, v in ff["hw_encoders_tested"].items()
    )
    table.add_row("Encoder H.264", f"[green]{ff['selected_encoder']}[/green]", enc_summary)

    wh = report["whisper"]
    table.add_row(
        "Whisper Local",
        f"[green]{wh['resolved_device']} ({wh['resolved_compute_type']})[/green]",
        f"Borrador={wh['draft_model']} | Precisión={wh['precision_model']} — {wh['note']}",
    )

    table.add_row(
        "Fuentes incluidas",
        "[green]OK[/green]" if report["bundled_fonts"] else "[yellow]VACÍO[/yellow]",
        ", ".join(report["bundled_fonts"]) or "Sin fuentes en profiles/_fonts",
    )
    console.print(table)

    if not skip_api:
        apis = report["apis"]
        api_table = Table(title="APIs Externas (.env)", show_header=True, header_style="bold magenta")
        api_table.add_column("Servicio", style="cyan")
        api_table.add_column("Estado")
        api_table.add_column("Detalle")

        or_info = apis["openrouter"]
        api_table.add_row(
            f"OpenRouter Fast ({or_info['fast_model']})",
            "[green]OK[/green]" if or_info["fast_ok"] else "[red]ERROR[/red]",
            or_info["fast_detail"],
        )
        api_table.add_row(
            f"OpenRouter Editorial ({or_info['editorial_model']})",
            "[green]OK[/green]" if or_info["editorial_ok"] else "[red]ERROR[/red]",
            or_info["editorial_detail"],
        )
        api_table.add_row(
            "Pexels API",
            "[green]OK[/green]" if apis["pexels"]["ok"] else "[yellow]AVISO[/yellow]",
            apis["pexels"]["detail"],
        )
        api_table.add_row(
            "Pixabay API",
            "[green]OK[/green]" if apis["pixabay"]["ok"] else "[yellow]AVISO[/yellow]",
            apis["pixabay"]["detail"],
        )
        console.print()
        console.print(api_table)


@profiles_app.command("list")
def profiles_list() -> None:
    """Lista los perfiles de podcaster configurados."""
    store = ProfileStore()
    profiles = store.list_profiles()
    table = Table(title="Perfiles Disponibles", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="green")
    table.add_column("Nombre")
    table.add_column("Podcast")
    table.add_column("Cuotas (Cliff / Concl / Med / Larg)")
    table.add_column("Aspectos")

    for p in profiles:
        quotas = (
            f"{p.quotas.shorts_cliffhanger_count} / "
            f"{p.quotas.shorts_conclusive_count} / "
            f"{p.quotas.medium_videos_count} / "
            f"{p.quotas.long_videos_count}"
        )
        table.add_row(p.id, p.name, p.podcast_name, quotas, ", ".join(p.visual.active_aspects))

    console.print(table)


@app.command("run")
def run_job(
    profile: str = typer.Option(..., "--profile", "-p", help="ID del perfil de podcaster."),
    input_file: Path = typer.Option(..., "--input", "-i", help="Ruta al archivo de audio del podcast."),
    episode_name: Optional[str] = typer.Option(None, "--episode-name", help="Nombre personalizado del episodio."),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", help="Carpeta de salida exacta (p. ej. output/prueba5)."),
    shorts_cliffhanger: Optional[int] = typer.Option(None, "--shorts-cliffhanger", help="Cantidad de Shorts con cliffhanger."),
    shorts_conclusive: Optional[int] = typer.Option(None, "--shorts-conclusive", help="Cantidad de Shorts conclusivos."),
    medium: Optional[int] = typer.Option(None, "--medium", help="Cantidad de vídeos medios (5-6 min)."),
    long: Optional[int] = typer.Option(None, "--long", help="Cantidad de vídeos largos (12 min)."),
    auto: bool = typer.Option(True, "--auto/--review", help="Ejecuta hasta el render final o pausa tras storyboard."),
    visual_hook: Optional[bool] = typer.Option(None, "--visual-hook/--no-visual-hook", help="Activa o desactiva el gancho visual a pantalla completa."),
    visual_hook_style: Optional[str] = typer.Option(None, "--visual-hook-style", help="Estilo del gancho visual (kinetic_fullscreen, editorial_card, bold_minimal)."),
    media_providers: Optional[str] = typer.Option(None, "--media-providers", help="Proveedores visuales separados por coma (custom_images,pexels,pixabay,wikimedia_commons,internet_archive)."),
    save_analytics: bool = typer.Option(False, "--save-analytics/--no-save-analytics", help="Guarda informe interactivo de análisis IA en analytics_studio/."),
) -> None:
    """Ejecuta el pipeline completo sobre un podcast."""
    if not input_file.exists():
        console.print(f"[red]Archivo de audio no encontrado: {input_file}[/red]")
        raise typer.Exit(code=1)

    parsed_provs = None
    if media_providers:
        from backend.models.profile import DEFAULT_MEDIA_PROVIDERS

        valid = set(DEFAULT_MEDIA_PROVIDERS)
        items = [x.strip() for x in media_providers.split(",") if x.strip() in valid]
        if items:
            parsed_provs = items  # type: ignore[assignment]

    jm = JobManager(get_settings())
    job = jm.create_job(
        profile_id=profile,
        source_audio_path=input_file,
        episode_name=(episode_name.strip() if episode_name else None) or input_file.stem,
        shorts_cliffhanger=shorts_cliffhanger,
        shorts_conclusive=shorts_conclusive,
        medium_count=medium,
        long_count=long,
        auto_mode=auto,
        visual_hook_enabled=visual_hook,
        visual_hook_style=visual_hook_style,
        allowed_media_providers=parsed_provs,
        output_dir_override=output_dir.strip() if output_dir else None,
        save_analytics_report=save_analytics,
    )
    console.print(f"[bold cyan]Trabajo creado:[/bold cyan] {job.job_id}")
    final_job = jm.run_job_sync(
        job.job_id,
        log_cb=lambda msg: console.print(f"  [cyan]\\[{time.strftime('%H:%M:%S')}][/cyan] {rich_escape(msg)}"),
    )

    if final_job.status == "failed":
        console.print(f"[red]Error: {final_job.error_message}[/red]")
        raise typer.Exit(code=1)

    if final_job.status == "awaiting_review":
        console.print(f"\n[yellow]Pausado para revisión.[/yellow] Edita desde la Web o ejecuta: [bold]python cli.py resume --job {final_job.job_id}[/bold]")
        return

    console.print(f"\n[bold green]¡Completado![/bold green] Salida en: [cyan]{final_job.output_dir}[/cyan]")
    console.print(
        f"Coste OpenRouter: ${final_job.cost.cost_usd:.4f} USD (~{final_job.cost.cost_eur:.4f} EUR) "
        f"| Tokens: {final_job.cost.total_tokens:,}"
    )


@app.command("resume")
def resume_job(
    job: str = typer.Option(..., "--job", "-j", help="ID del trabajo a reanudar."),
    approve: bool = typer.Option(True, "--approve/--keep-mode", help="Si estaba en revisión, aprueba y renderiza."),
) -> None:
    """Reanuda un trabajo interrumpido o en revisión desde su último checkpoint."""
    jm = JobManager(get_settings())
    state = jm.load_job(job)
    if state.status == "awaiting_review" and approve:
        state.auto_mode = True
        jm._save_job(state)
    console.print(f"[bold cyan]Reanudando trabajo:[/bold cyan] {job}")
    final_job = jm.run_job_sync(job, log_cb=lambda msg: console.print(f"  [dim]•[/dim] {rich_escape(msg)}"))
    if final_job.status == "failed":
        console.print(f"[red]Error: {final_job.error_message}[/red]")
        raise typer.Exit(code=1)
    console.print(f"\n[bold green]Estado final:[/bold green] {final_job.status} | Salida: {final_job.output_dir}")


@app.command("watch")
def watch_folder(
    profile: str = typer.Option(..., "--profile", "-p", help="ID del perfil de podcaster."),
    process_existing: bool = typer.Option(False, "--process-existing", help="Procesar audios ya presentes al iniciar."),
) -> None:
    """Vigila la carpeta input/ para procesar automáticamente nuevos audios."""
    settings = get_settings()
    jm = JobManager(settings)
    watcher = FolderWatcher(jm, settings)
    st = watcher.start(profile_id=profile, process_existing=process_existing)
    console.print(f"[bold green]Vigilando carpeta:[/bold green] {st['input_dir']} (perfil: {profile})")
    console.print("Pulsa Ctrl+C para detener.")
    try:
        while True:
            time.sleep(1.5)
    except KeyboardInterrupt:
        watcher.stop()
        console.print("\n[yellow]Vigilancia detenida.[/yellow]")


if __name__ == "__main__":
    app()
