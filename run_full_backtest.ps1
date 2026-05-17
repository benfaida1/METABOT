# =============================================================================
# run_full_backtest.ps1
#
# Lance le backtest 12 mois sur 10 paires x 4 strategies AVEC une window
# enorme (99999, soit "tout l'historique"), MAIS en separant chaque
# combinaison symbole/strategie pour eviter d'exploser la RAM.
#
# Idee :
#   - 40 mini-runs sequentiels (10 symboles x 4 strategies)
#   - Chaque run = 1 symbole + 1 strategie -> RAM ~2-3 GB, dure 5-30 min
#   - Entre chaque run, Python se ferme et libere TOUTE la RAM
#   - Resultat sauve dans results/result_SYMBOL_STRAT.json
#   - Reprise possible : si un fichier existe deja, on saute (idempotent)
#
# Usage :
#   .\run_full_backtest.ps1                  # full 12 mois, window 99999
#   .\run_full_backtest.ps1 -Jours 90        # 3 mois au lieu de 12
#   .\run_full_backtest.ps1 -Window 200      # window normale (rapide)
#   .\run_full_backtest.ps1 -Pause 60        # 60s entre chaque run (etale)
# =============================================================================

param(
    [int]$Jours  = 365,
    [int]$Window = 99999,
    [int]$Pause  = 0
)

$ErrorActionPreference = "Stop"

# ---- Configuration ----------------------------------------------------------
$Symboles = @(
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "ADAUSDT", "DOTUSDT"
)

$Strategies = @(
    "TREND_FOLLOW", "PULLBACK", "BREAKOUT", "MEAN_REVERSION"
)

$ResultsDir = "results"
$LogFile    = "run_full_backtest.log"

# Detecter python ou python3
$Python = "python"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    $Python = "python3"
}

# ---- Preparation ------------------------------------------------------------
if (-not (Test-Path $ResultsDir)) {
    New-Item -ItemType Directory -Path $ResultsDir | Out-Null
}

$Total = $Symboles.Count * $Strategies.Count
$Done  = 0
$Skipped = 0
$Failed = 0
$StartGlobal = Get-Date

function Write-Log {
    param([string]$Msg)
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$stamp] $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

Write-Log "================================================================"
Write-Log "BACKTEST COMPLET - Split par symbole x strategie"
Write-Log "Jours=$Jours  Window=$Window  Pause=${Pause}s"
Write-Log "Symboles : $($Symboles -join ', ')"
Write-Log "Strategies : $($Strategies -join ', ')"
Write-Log "Total = $Total runs"
Write-Log "================================================================"

# ---- Boucle principale ------------------------------------------------------
$Index = 0
foreach ($sym in $Symboles) {
    foreach ($strat in $Strategies) {
        $Index++
        $jsonFile = Join-Path $ResultsDir "result_${sym}_${strat}.json"

        # Idempotence : skip si deja fait
        if (Test-Path $jsonFile) {
            $Skipped++
            Write-Log "[$Index/$Total] SKIP (deja fait) $sym $strat"
            continue
        }

        Write-Log "[$Index/$Total] START $sym $strat ..."
        $tStart = Get-Date

        # Lancement Python - on capture stderr pour detecter les erreurs
        try {
            & $Python backtest.py `
                --jours $Jours `
                --symboles $sym `
                --strategie $strat `
                --window-15m $Window `
                --window-1h $Window `
                --json $jsonFile 2>&1 | Out-Null

            if ($LASTEXITCODE -ne 0) {
                throw "exit code $LASTEXITCODE"
            }
        } catch {
            $Failed++
            Write-Log "[$Index/$Total] FAIL   $sym $strat : $_"
            continue
        }

        $tEnd = Get-Date
        $dur = [int]($tEnd - $tStart).TotalSeconds
        $Done++

        # Lecture du JSON pour afficher le nb de trades
        try {
            $r = Get-Content $jsonFile -Raw | ConvertFrom-Json
            $nb = $r.trades.Count
        } catch {
            $nb = "?"
        }

        $elapsed = [int]((Get-Date) - $StartGlobal).TotalMinutes
        Write-Log "[$Index/$Total] DONE   $sym $strat -> $nb trades en ${dur}s  (total ecoule: ${elapsed} min)"

        if ($Pause -gt 0 -and $Index -lt $Total) {
            Start-Sleep -Seconds $Pause
        }
    }
}

# ---- Resume final -----------------------------------------------------------
$EndGlobal = Get-Date
$TotalMin  = [int]($EndGlobal - $StartGlobal).TotalMinutes

Write-Log "================================================================"
Write-Log "TERMINE en $TotalMin minutes"
Write-Log "  DONE    : $Done"
Write-Log "  SKIPPED : $Skipped"
Write-Log "  FAILED  : $Failed"
Write-Log "================================================================"

# ---- Aggregation finale -----------------------------------------------------
Write-Log "Aggregation des resultats..."

$AllTrades = @()
foreach ($sym in $Symboles) {
    foreach ($strat in $Strategies) {
        $jsonFile = Join-Path $ResultsDir "result_${sym}_${strat}.json"
        if (-not (Test-Path $jsonFile)) { continue }
        try {
            $r = Get-Content $jsonFile -Raw | ConvertFrom-Json
            if ($r.trades) {
                $AllTrades += $r.trades
            }
        } catch {
            Write-Log "  ! Impossible de lire $jsonFile"
        }
    }
}

if ($AllTrades.Count -eq 0) {
    Write-Log "Aucun trade trouve. Verifie les logs ci-dessus."
    exit 1
}

# Stats par strategie
Write-Log ""
Write-Log "STATS PAR STRATEGIE"
Write-Log ("STRATEGIE          N      WR%   PNL_MOY%   TOTAL%   MAX_DD%")
Write-Log ("---------------------------------------------------------------")

$byStrat = $AllTrades | Group-Object -Property strategie
foreach ($g in $byStrat | Sort-Object Name) {
    $n      = $g.Count
    $wins   = ($g.Group | Where-Object { $_.pnl_pct -gt 0 }).Count
    $wr     = if ($n -gt 0) { 100.0 * $wins / $n } else { 0 }
    $sumPnl = ($g.Group | Measure-Object -Property pnl_pct -Sum).Sum
    $avgPnl = if ($n -gt 0) { $sumPnl / $n } else { 0 }

    # Max drawdown approximatif (equity curve)
    $sorted = $g.Group | Sort-Object -Property entry_time
    $eq = 0; $peak = 0; $maxDD = 0
    foreach ($t in $sorted) {
        $eq += $t.pnl_pct
        if ($eq -gt $peak) { $peak = $eq }
        $dd = $peak - $eq
        if ($dd -gt $maxDD) { $maxDD = $dd }
    }

    $line = "{0,-18} {1,5} {2,7:F1}% {3,9:F3}% {4,8:F2}% {5,8:F2}%" -f `
        $g.Name, $n, $wr, $avgPnl, $sumPnl, $maxDD
    Write-Log $line
}

Write-Log ""
Write-Log "Detail complet par symbole x strategie : $ResultsDir/*.json"
Write-Log "Aggregation JSON globale : backtest_FULL_aggregated.json"

# Sauvegarde l'agregation globale
$agg = @{
    jours = $Jours
    window_15m = $Window
    window_1h  = $Window
    symboles = $Symboles
    strategies = $Strategies
    total_trades = $AllTrades.Count
    duree_minutes = $TotalMin
    trades = $AllTrades
}
$agg | ConvertTo-Json -Depth 5 | Out-File -FilePath "backtest_FULL_aggregated.json" -Encoding utf8

Write-Log "Fini. Tu peux maintenant fermer ce terminal."
