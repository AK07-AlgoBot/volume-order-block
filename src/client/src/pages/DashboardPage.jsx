import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiFetch, clearStoredAuth, getStoredAuth, getWsBase } from "../api/client";
import { ClosedTradesTable } from "../components/ClosedTradesTable";
import { LiveTradesTable } from "../components/LiveTradesTable";
import { SymbolPerformanceTable } from "../components/SymbolPerformanceTable";
import { WeeklyPnlChart } from "../components/WeeklyPnlChart";
import { OrdersLogPanel } from "../components/OrdersLogPanel";
import { PaperTradingPanel } from "../components/PaperTradingPanel";
import { TradingScriptsCard } from "../components/TradingScriptsCard";
import { OrderBlockCard } from "../components/OrderBlockCard";
import { UpstoxSettingsCard } from "../components/UpstoxSettingsCard";

function getLocalDateIso() {
  const now = new Date();
  const offsetMs = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offsetMs).toISOString().slice(0, 10);
}

function upsertTrade(list, trade) {
  const index = list.findIndex((item) => item.id === trade.id);
  if (index === -1) {
    return [trade, ...list];
  }
  const cloned = [...list];
  cloned[index] = { ...cloned[index], ...trade };
  return cloned;
}

function removeTrade(list, tradeId) {
  return list.filter((item) => item.id !== tradeId);
}

function normalizeIsoSecond(value) {
  const text = String(value || "").trim().replace(",", ".");
  return text.length >= 19 ? text.slice(0, 19) : text;
}

function liveTradeIdentityKey(trade) {
  return [
    trade?.symbol || "",
    trade?.side || "",
    Number(trade?.quantity || 0).toFixed(4),
    Number(trade?.entry_price || 0).toFixed(4),
    normalizeIsoSecond(trade?.opened_at),
  ].join("|");
}

function dedupeLiveTrades(trades) {
  const latestByKey = new Map();
  for (const trade of trades || []) {
    const key = liveTradeIdentityKey(trade);
    const prev = latestByKey.get(key);
    const rank = `${trade?.opened_at || ""}|${trade?.id || ""}`;
    const prevRank = prev ? `${prev?.opened_at || ""}|${prev?.id || ""}` : "";
    if (!prev || rank >= prevRank) {
      latestByKey.set(key, trade);
    }
  }
  return Array.from(latestByKey.values());
}

export default function DashboardPage() {
  const navigate = useNavigate();
  const auth = getStoredAuth();
  const { token, username } = auth;

  const [todayDateText] = useState(getLocalDateIso);
  const [liveTrades, setLiveTrades] = useState([]);
  const [closedTrades, setClosedTrades] = useState([]);
  const [closedTradeDates, setClosedTradeDates] = useState([]);
  const [selectedClosedDate, setSelectedClosedDate] = useState("");
  const [weeklyPnl, setWeeklyPnl] = useState([]);
  const [weeklyTotal, setWeeklyTotal] = useState(0);
  const [weeklyFilterOptions, setWeeklyFilterOptions] = useState([]);
  const [selectedWeekOffset, setSelectedWeekOffset] = useState(0);
  const [monthlyPnl, setMonthlyPnl] = useState([]);
  const [monthlyTotal, setMonthlyTotal] = useState(0);
  const [monthlyFilterOptions, setMonthlyFilterOptions] = useState([]);
  const [selectedMonthOffset, setSelectedMonthOffset] = useState(0);
  const [pnlChartMode, setPnlChartMode] = useState("week");
  const [istMonth, setIstMonth] = useState({ total: 0, range_start: "", range_end: "" });
  const [connected, setConnected] = useState(false);
  const [dataUserLabel, setDataUserLabel] = useState("");
  const [perfDays, setPerfDays] = useState(14);
  const [symbolPerfRows, setSymbolPerfRows] = useState([]);
  const [symbolPerfMeta, setSymbolPerfMeta] = useState({
    trade_count: 0,
    cutoff_date: "",
    end_date: "",
  });
  const [mainView, setMainView] = useState("dashboard");
  const [manualActionTradeId, setManualActionTradeId] = useState("");
  const selectedClosedDateRef = useRef(selectedClosedDate);
  const selectedWeekOffsetRef = useRef(selectedWeekOffset);
  const selectedMonthOffsetRef = useRef(selectedMonthOffset);
  const perfDaysRef = useRef(perfDays);
  const appReadySentRef = useRef(false);

  const markAppReady = () => {
    if (appReadySentRef.current) {
      return;
    }
    appReadySentRef.current = true;
    window.dispatchEvent(new Event("ak07-app-ready"));
  };

  useEffect(() => {
    if (!token) {
      navigate("/login", { replace: true });
    }
  }, [token, navigate]);

  useEffect(() => {
    selectedClosedDateRef.current = selectedClosedDate;
  }, [selectedClosedDate]);

  useEffect(() => {
    selectedWeekOffsetRef.current = selectedWeekOffset;
  }, [selectedWeekOffset]);

  useEffect(() => {
    selectedMonthOffsetRef.current = selectedMonthOffset;
  }, [selectedMonthOffset]);

  useEffect(() => {
    perfDaysRef.current = perfDays;
  }, [perfDays]);

  useEffect(() => {
    if (!token) {
      return undefined;
    }
    let active = true;

    const loadWeeklyPnl = (weekOffset = selectedWeekOffsetRef.current) => {
      apiFetch(`/api/dashboard/weekly-pnl?week_offset=${encodeURIComponent(weekOffset)}`)
        .then((response) => response.json())
        .then((payload) => {
          if (!active) {
            return;
          }
          setWeeklyPnl(payload.weekly_pnl || []);
          setWeeklyTotal(Number(payload.weekly_total || 0));
          setWeeklyFilterOptions(payload.weekly_filter_options || []);
          setSelectedWeekOffset(Number(payload.weekly_selected_offset || 0));
          if (payload.ist_month) {
            setIstMonth(payload.ist_month);
          }
        })
        .catch((error) => {
          console.error("Failed weekly pnl load", error);
        });
    };

    const loadMonthlyPnl = (monthOffset = selectedMonthOffsetRef.current) => {
      apiFetch(`/api/dashboard/monthly-pnl?month_offset=${encodeURIComponent(monthOffset)}`)
        .then((response) => response.json())
        .then((payload) => {
          if (!active) {
            return;
          }
          setMonthlyPnl(payload.monthly_pnl || []);
          setMonthlyTotal(Number(payload.monthly_total || 0));
          setMonthlyFilterOptions(payload.monthly_filter_options || []);
          setSelectedMonthOffset(Number(payload.monthly_selected_offset || 0));
          if (payload.ist_month) {
            setIstMonth(payload.ist_month);
          }
        })
        .catch((error) => {
          console.error("Failed monthly pnl load", error);
        });
    };

    const loadSymbolPerformance = () => {
      const days = perfDaysRef.current;
      apiFetch(`/api/dashboard/symbol-performance?days=${encodeURIComponent(days)}`)
        .then((response) => response.json())
        .then((payload) => {
          if (!active) {
            return;
          }
          setSymbolPerfRows(payload.rows || []);
          setSymbolPerfMeta({
            trade_count: Number(payload.trade_count || 0),
            cutoff_date: String(payload.cutoff_date || ""),
            end_date: String(payload.end_date || ""),
          });
        })
        .catch((error) => {
          console.error("Failed symbol performance load", error);
        });
    };

    apiFetch("/api/dashboard/initial")
      .then((response) => {
        if (response.status === 401) {
          clearStoredAuth();
          navigate("/login", { replace: true });
          return null;
        }
        return response.json();
      })
      .then((payload) => {
        if (!active || !payload) {
          return;
        }
        setLiveTrades(dedupeLiveTrades(payload.live_trades || []));
        setClosedTrades(payload.closed_trades || []);
        setClosedTradeDates(payload.closed_trade_dates || []);
        setSelectedClosedDate(payload.closed_trade_selected_date || todayDateText);
        setWeeklyPnl(payload.weekly_pnl || []);
        setWeeklyTotal(Number(payload.weekly_total || 0));
        setWeeklyFilterOptions(payload.weekly_filter_options || []);
        setSelectedWeekOffset(Number(payload.weekly_selected_offset || 0));
        setMonthlyPnl(payload.monthly_pnl || []);
        setMonthlyTotal(Number(payload.monthly_total || 0));
        setMonthlyFilterOptions(payload.monthly_filter_options || []);
        setSelectedMonthOffset(Number(payload.monthly_selected_offset || 0));
        if (payload.ist_month) {
          setIstMonth(payload.ist_month);
        }
        setDataUserLabel(String(payload.data_user || ""));
        markAppReady();
      })
      .catch((error) => {
        console.error("Failed initial load", error);
        markAppReady();
      });

    loadSymbolPerformance();
    loadMonthlyPnl();

    const wsBase = getWsBase();
    const q = new URLSearchParams({ token });
    const socket = new WebSocket(`${wsBase}/ws/trades?${q.toString()}`);
    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onerror = () => setConnected(false);
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "trade_opened" || message.type === "trade_updated") {
          setLiveTrades((prev) => dedupeLiveTrades(upsertTrade(prev, message.trade)));
          return;
        }

        if (message.type === "trades_updated_batch") {
          setLiveTrades((prev) =>
            dedupeLiveTrades(
              message.trades.reduce((acc, t) => upsertTrade(acc, t), prev)
            )
          );
          return;
        }

        if (message.type === "trade_closed") {
          setLiveTrades((prev) =>
            dedupeLiveTrades(removeTrade(prev, message.trade.id)).filter(
              (item) => liveTradeIdentityKey(item) !== liveTradeIdentityKey(message.trade)
            )
          );
          apiFetch(
            `/api/dashboard/closed-trades?date=${encodeURIComponent(
              selectedClosedDateRef.current
            )}`
          )
            .then((response) => response.json())
            .then((payload) => {
              if (!active) {
                return;
              }
              setClosedTrades(payload.closed_trades || []);
              setClosedTradeDates(payload.closed_trade_dates || []);
            })
            .catch((error) => {
              console.error("Failed closed trades refresh", error);
            });
          loadWeeklyPnl();
          loadMonthlyPnl();
          loadSymbolPerformance();
          return;
        }

        if (message.type === "pnl_update") {
          if (message.ist_month) {
            setIstMonth(message.ist_month);
          }
          if (selectedWeekOffsetRef.current === 0) {
            setWeeklyPnl(message.weekly_pnl || []);
            const total = (message.weekly_pnl || []).reduce(
              (sum, point) => sum + Number(point?.pnl || 0),
              0
            );
            setWeeklyTotal(total);
          }
          if (selectedMonthOffsetRef.current === 0) {
            loadMonthlyPnl(0);
          }
        }
      } catch (error) {
        console.error("WebSocket parse failed", error);
      }
    };

    const keepAlive = window.setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send("ping");
      }
    }, 15000);
    const weeklyRefresh = window.setInterval(() => {
      loadWeeklyPnl();
    }, 30000);
    const monthlyRefresh = window.setInterval(() => {
      loadMonthlyPnl();
    }, 30000);
    const perfRefresh = window.setInterval(() => {
      loadSymbolPerformance();
    }, 120000);

    return () => {
      active = false;
      window.clearInterval(keepAlive);
      window.clearInterval(weeklyRefresh);
      window.clearInterval(monthlyRefresh);
      window.clearInterval(perfRefresh);
      socket.close();
    };
  }, [todayDateText, token, navigate]);

  useEffect(() => {
    if (!token || !selectedClosedDate) {
      return undefined;
    }
    let active = true;
    apiFetch(`/api/dashboard/weekly-pnl?week_offset=${encodeURIComponent(selectedWeekOffset)}`)
      .then((response) => response.json())
      .then((payload) => {
        if (!active) {
          return;
        }
        setWeeklyPnl(payload.weekly_pnl || []);
        setWeeklyTotal(Number(payload.weekly_total || 0));
        setWeeklyFilterOptions(payload.weekly_filter_options || []);
        if (payload.ist_month) {
          setIstMonth(payload.ist_month);
        }
      })
      .catch((error) => {
        console.error("Failed selected weekly pnl load", error);
      });
    return () => {
      active = false;
    };
  }, [selectedWeekOffset, token]);

  useEffect(() => {
    if (!token) {
      return undefined;
    }
    let active = true;
    apiFetch(`/api/dashboard/monthly-pnl?month_offset=${encodeURIComponent(selectedMonthOffset)}`)
      .then((response) => response.json())
      .then((payload) => {
        if (!active) {
          return;
        }
        setMonthlyPnl(payload.monthly_pnl || []);
        setMonthlyTotal(Number(payload.monthly_total || 0));
        setMonthlyFilterOptions(payload.monthly_filter_options || []);
        if (payload.ist_month) {
          setIstMonth(payload.ist_month);
        }
      })
      .catch((error) => {
        console.error("Failed selected monthly pnl load", error);
      });
    return () => {
      active = false;
    };
  }, [selectedMonthOffset, token]);

  useEffect(() => {
    if (!token) {
      return undefined;
    }
    let active = true;
    apiFetch(`/api/dashboard/closed-trades?date=${encodeURIComponent(selectedClosedDate)}`)
      .then((response) => response.json())
      .then((payload) => {
        if (!active) {
          return;
        }
        setClosedTrades(payload.closed_trades || []);
        setClosedTradeDates(payload.closed_trade_dates || []);
      })
      .catch((error) => {
        console.error("Failed closed trades load", error);
      });
    return () => {
      active = false;
    };
  }, [selectedClosedDate, token]);

  useEffect(() => {
    if (!token) {
      return undefined;
    }
    let active = true;
    apiFetch(`/api/dashboard/symbol-performance?days=${encodeURIComponent(perfDays)}`)
      .then((response) => response.json())
      .then((payload) => {
        if (!active) {
          return;
        }
        setSymbolPerfRows(payload.rows || []);
        setSymbolPerfMeta({
          trade_count: Number(payload.trade_count || 0),
          cutoff_date: String(payload.cutoff_date || ""),
          end_date: String(payload.end_date || ""),
        });
      })
      .catch((error) => {
        console.error("Failed symbol performance load", error);
      });
    return () => {
      active = false;
    };
  }, [perfDays, token]);

  const todayRealized = useMemo(
    () => Number(weeklyPnl.find((point) => point.date === todayDateText)?.pnl || 0),
    [weeklyPnl, todayDateText]
  );

  const onLogout = () => {
    clearStoredAuth();
    navigate("/login", { replace: true });
  };

  const onEditManualEntry = async (trade) => {
    const currentEntry = Number(trade?.entry_price || 0);
    const next = window.prompt(
      `Manual entry price for ${trade?.symbol || ""}:`,
      Number.isFinite(currentEntry) ? currentEntry.toFixed(2) : ""
    );
    if (next === null) return;
    const parsed = Number(next);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      window.alert("Enter a valid positive price.");
      return;
    }
    setManualActionTradeId(trade.id);
    try {
      const res = await apiFetch("/api/dashboard/manual-trade/update-entry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trade_id: trade.id, entry_price: parsed }),
      });
      const payload = await res.json();
      if (!res.ok) {
        throw new Error(payload?.detail || "Failed to update manual entry");
      }
      setLiveTrades((prev) => dedupeLiveTrades(upsertTrade(prev, payload.trade || {})));
    } catch (e) {
      window.alert(e?.message || "Failed to update manual entry");
    } finally {
      setManualActionTradeId("");
    }
  };

  const onRemoveManualTrade = async (trade) => {
    const ok = window.confirm(
      `Remove manual trade ${trade?.symbol || ""}? This hides it from dashboard and P&L.`
    );
    if (!ok) return;
    setManualActionTradeId(trade.id);
    try {
      const res = await apiFetch("/api/dashboard/manual-trade/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trade_id: trade.id }),
      });
      const payload = await res.json();
      if (!res.ok) {
        throw new Error(payload?.detail || "Failed to remove manual trade");
      }
      setLiveTrades((prev) => dedupeLiveTrades(removeTrade(prev, trade.id)));
    } catch (e) {
      window.alert(e?.message || "Failed to remove manual trade");
    } finally {
      setManualActionTradeId("");
    }
  };

  const onEditClosedTrade = async (trade) => {
    const nextEntry = window.prompt(
      `Entry price for ${trade?.symbol || ""}:`,
      Number(trade?.entry_price || 0).toFixed(2)
    );
    if (nextEntry === null) return;
    const nextExit = window.prompt(
      `Exit price for ${trade?.symbol || ""}:`,
      Number(trade?.exit_price || 0).toFixed(2)
    );
    if (nextExit === null) return;
    const e = Number(nextEntry);
    const x = Number(nextExit);
    if (!Number.isFinite(e) || e <= 0 || !Number.isFinite(x) || x <= 0) {
      window.alert("Enter valid positive entry and exit prices.");
      return;
    }
    try {
      const res = await apiFetch("/api/dashboard/manual-trade/update-closed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trade_id: trade.id, entry_price: e, exit_price: x }),
      });
      const payload = await res.json();
      if (!res.ok) {
        throw new Error(payload?.detail || "Failed to update closed trade prices");
      }
      apiFetch(`/api/dashboard/closed-trades?date=${encodeURIComponent(selectedClosedDateRef.current)}`)
        .then((r) => r.json())
        .then((p) => {
          setClosedTrades(p.closed_trades || []);
          setClosedTradeDates(p.closed_trade_dates || []);
        });
      apiFetch(`/api/dashboard/weekly-pnl?week_offset=${encodeURIComponent(selectedWeekOffsetRef.current)}`)
        .then((r) => r.json())
        .then((p) => {
          setWeeklyPnl(p.weekly_pnl || []);
          setWeeklyTotal(Number(p.weekly_total || 0));
          setWeeklyFilterOptions(p.weekly_filter_options || []);
          if (p.ist_month) setIstMonth(p.ist_month);
        });
      apiFetch(`/api/dashboard/monthly-pnl?month_offset=${encodeURIComponent(selectedMonthOffsetRef.current)}`)
        .then((r) => r.json())
        .then((p) => {
          setMonthlyPnl(p.monthly_pnl || []);
          setMonthlyTotal(Number(p.monthly_total || 0));
          setMonthlyFilterOptions(p.monthly_filter_options || []);
        });
    } catch (err) {
      window.alert(err?.message || "Failed to update closed trade prices");
    }
  };

  if (!token) {
    return null;
  }

  return (
    <>
      <header className="header">
        <div className="container header-inner">
          <div className="logo-wrap">
            <div className="logo-box">AK07</div>
            <div>
              <div className="tag">Trading Dashboard</div>
              <h1 className="title">AK07 Live Monitor</h1>
            </div>
          </div>
          <div className="header-actions">
            <span className="user-chip">
              {username}
              {dataUserLabel && dataUserLabel !== username ? (
                <span className="subtle"> · data: {dataUserLabel}</span>
              ) : null}
            </span>
            <span className="status-chip">
              <span className="dot" />
              {connected ? "Live Connected" : "Disconnected"}
            </span>
            <nav className="header-nav-tabs" aria-label="Main views">
              <button
                type="button"
                className={`nav-tab ${mainView === "dashboard" ? "nav-tab-active" : ""}`}
                onClick={() => setMainView("dashboard")}
              >
                Dashboard
              </button>
              <button
                type="button"
                className={`nav-tab ${mainView === "ordersLog" ? "nav-tab-active" : ""}`}
                onClick={() => setMainView("ordersLog")}
              >
                Orders log
              </button>
              <button
                type="button"
                className={`nav-tab ${mainView === "paperTrading" ? "nav-tab-active" : ""}`}
                onClick={() => setMainView("paperTrading")}
              >
                Paper P&L
              </button>
            </nav>
            <button type="button" className="btn-logout" onClick={onLogout}>
              Log out
            </button>
          </div>
        </div>
      </header>

      <main className="container">
        <OrdersLogPanel active={mainView === "ordersLog"} />
        <PaperTradingPanel active={mainView === "paperTrading"} />
        <div
          className={`dashboard-stack ${
            mainView === "dashboard" ? "" : "dashboard-stack-hidden"
          }`}
        >
        <div className="layout">
          <LiveTradesTable
            trades={liveTrades}
            onEditManualEntry={onEditManualEntry}
            onRemoveManualTrade={onRemoveManualTrade}
            busyTradeId={manualActionTradeId}
          />
          <WeeklyPnlChart
            chartMode={pnlChartMode}
            onChartModeChange={setPnlChartMode}
            istMonth={istMonth}
            weekPoints={weeklyPnl}
            weekTotal={weeklyTotal}
            weekOptions={weeklyFilterOptions}
            weekOffset={selectedWeekOffset}
            onWeekChange={setSelectedWeekOffset}
            monthPoints={monthlyPnl}
            monthTotal={monthlyTotal}
            monthOptions={monthlyFilterOptions}
            monthOffset={selectedMonthOffset}
            onMonthChange={setSelectedMonthOffset}
          />
        </div>

        <div className="closed-section">
          <ClosedTradesTable
            trades={closedTrades}
            availableDates={closedTradeDates}
            selectedDate={selectedClosedDate}
            onDateChange={setSelectedClosedDate}
            onEditClosedTrade={onEditClosedTrade}
          />
        </div>

        <div className="symbol-perf-section">
          <SymbolPerformanceTable
            rows={symbolPerfRows}
            periodDays={perfDays}
            onPeriodChange={setPerfDays}
            tradeCount={symbolPerfMeta.trade_count}
            cutoffDate={symbolPerfMeta.cutoff_date}
            endDate={symbolPerfMeta.end_date}
          />
        </div>

        <TradingScriptsCard />

        <UpstoxSettingsCard />

        <OrderBlockCard />

        <div className="subtle" style={{ marginTop: "0.75rem", marginBottom: "1.5rem" }}>
          Today realized P&L: {todayRealized.toFixed(2)}
        </div>
        </div>
      </main>
    </>
  );
}
