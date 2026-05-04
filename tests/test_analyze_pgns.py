from scripts.analyze_pgns import aggregate, load_games


def test_analyzer_reports_phase_fallback_rates(tmp_path):
    pgn = tmp_path / "phase.pgn"
    pgn.write_text(
        "\n".join(
            [
                '[Event "Local BOT Dry Run"]',
                '[Site "Local"]',
                '[Result "*"]',
                '[Termination "max plies reached"]',
                '[PlyCount "4"]',
                "",
                '1. e4 { clk_before 500ms; clk_after 495ms; think 4.2ms; wall 5.0ms; charged 4ms; eval 10; source stockfish; blunder safe }',
                '1... e5 { clk_before 500ms; clk_after 497ms; think 3.1ms; wall 4.0ms; charged 3ms; eval 0; source forcing-fallback; blunder safe }',
                '2. Nf3 { clk_before 495ms; clk_after 491ms; think 4.0ms; wall 4.4ms; charged 4ms; eval 20; source least-bad; blunder safe }',
                '2... Nc6 { clk_before 497ms; clk_after 493ms; think 4.0ms; wall 4.5ms; charged 4ms; eval 5; source stockfish; blunder safe } *',
            ]
        ),
        encoding="utf-8",
    )
    summary = aggregate(load_games(tmp_path, []), min_stockfish_pct=1)
    assert "fallback_rate_by_phase" in summary
    assert summary["fallback_rate_by_phase"]["opening"] == 25.0
    assert summary["average_charged_ms"] > 0
    assert summary["average_wall_elapsed_ms"] > 0
