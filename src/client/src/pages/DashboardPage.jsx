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
    if (!token || !selectedClosedDate) {
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
          <LiveTradesTable trades={liveTrades} />
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

        <div className="subtle" style={{ marginTop: "0.75rem", marginBottom: "1.5rem" }}>
          Today realized P&L: {todayRealized.toFixed(2)}
        </div>
        </div>
      </main>
    </>
  );
}
