import React, { useCallback, useEffect, useState } from 'react';
import {
  ApiError,
  apiRequest,
  clearStoredToken,
  getStoredToken,
  PrincipalInfo,
} from './api/client';
import { AuthGate } from './components/AuthGate';
import { Layout, NavPage } from './components/Layout';
import { OverviewPage } from './pages/OverviewPage';
import { MindPage } from './pages/MindPage';
import { DrivesPage } from './pages/DrivesPage';
import { TasksPage } from './pages/TasksPage';
import { MemoryPage } from './pages/MemoryPage';
import { WorldPage } from './pages/WorldPage';
import { SelfModelPage } from './pages/SelfModelPage';
import { GovernancePage } from './pages/GovernancePage';
import { ConfigPage } from './pages/ConfigPage';
import { LogsPage } from './pages/LogsPage';

export function App() {
  const [token, setToken] = useState<string>(() => getStoredToken());
  const [principal, setPrincipal] = useState<PrincipalInfo | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  const [activePage, setActivePage] = useState<NavPage>('overview');
  const [windowHours, setWindowHours] = useState<number>(24);
  const [pollSeconds, setPollSeconds] = useState<number>(30);
  const [refreshTick, setRefreshTick] = useState<number>(0);
  const [lastUpdated, setLastUpdated] = useState<string>('—');

  const [darkMode, setDarkMode] = useState<boolean>(() => {
    try {
      const saved = localStorage.getItem('aoc_cc_theme');
      return saved ? saved === 'dark' : true;
    } catch {
      return true;
    }
  });

  // Cross-page navigation targets (Cycle <-> Task)
  const [selectedCycleId, setSelectedCycleId] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);

  // Overview snapshot shared with sidebar header badge
  const [overview, setOverview] = useState<any>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewErr, setOverviewErr] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode);
    try {
      localStorage.setItem('aoc_cc_theme', darkMode ? 'dark' : 'light');
    } catch {
      // ignore
    }
  }, [darkMode]);

  // Verify stored token on startup
  useEffect(() => {
    if (!token) {
      setPrincipal(null);
      setAuthChecked(true);
      return;
    }
    let active = true;
    apiRequest<PrincipalInfo>('/v2/auth/me', { token })
      .then((p) => {
        if (!active) return;
        setPrincipal(p);
      })
      .catch(() => {
        if (!active) return;
        clearStoredToken();
        setToken('');
        setPrincipal(null);
      })
      .finally(() => active && setAuthChecked(true));
    return () => {
      active = false;
    };
  }, [token]);

  const fetchOverview = useCallback(() => {
    if (!principal) return;
    setOverviewLoading(true);
    apiRequest(`/v2/control-center/overview?hours=${windowHours}`)
      .then((res) => {
        setOverview(res);
        setOverviewErr(null);
        setLastUpdated(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          clearStoredToken();
          setToken('');
          setPrincipal(null);
          return;
        }
        setOverviewErr(e?.detail || e?.message || '请求失败');
      })
      .finally(() => setOverviewLoading(false));
  }, [principal, windowHours]);

  useEffect(() => {
    fetchOverview();
  }, [fetchOverview, refreshTick]);

  // Configurable gentle auto-polling (stops automatically on unmount / logout)
  useEffect(() => {
    if (!principal || pollSeconds <= 0) return;
    const id = window.setInterval(() => {
      setRefreshTick((t) => t + 1);
    }, pollSeconds * 1000);
    return () => window.clearInterval(id);
  }, [principal, pollSeconds]);

  const handleOpenCycle = (cycleId: string) => {
    setSelectedCycleId(cycleId);
    setActivePage('mind');
  };

  const handleOpenTask = (taskId: string) => {
    setSelectedTaskId(taskId);
    setActivePage('tasks');
  };

  if (!authChecked) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950 text-xs text-slate-400">
        正在校验会话凭证…
      </div>
    );
  }

  if (!principal) {
    return (
      <AuthGate
        onAuthenticated={(tok, p) => {
          setToken(tok);
          setPrincipal(p);
          setAuthChecked(true);
        }}
      />
    );
  }

  return (
    <Layout
      activePage={activePage}
      onNavigate={setActivePage}
      principal={principal}
      lifecycleState={overview?.organism?.lifecycle_state}
      organismName={overview?.organism?.name}
      windowHours={windowHours}
      onWindowHoursChange={setWindowHours}
      pollSeconds={pollSeconds}
      onPollSecondsChange={setPollSeconds}
      onManualRefresh={() => setRefreshTick((t) => t + 1)}
      lastUpdated={lastUpdated}
      darkMode={darkMode}
      onToggleDark={() => setDarkMode((d) => !d)}
      onLogout={() => {
        clearStoredToken();
        setToken('');
        setPrincipal(null);
        setOverview(null);
      }}
    >
      {activePage === 'overview' && (
        <OverviewPage
          data={overview}
          loading={overviewLoading}
          error={overviewErr}
          onRefresh={() => setRefreshTick((t) => t + 1)}
          principal={principal}
          onOpenCycle={handleOpenCycle}
          onOpenTask={handleOpenTask}
        />
      )}
      {activePage === 'mind' && (
        <MindPage
          windowHours={windowHours}
          refreshTick={refreshTick}
          selectedCycleId={selectedCycleId}
          onSelectCycle={setSelectedCycleId}
          onOpenTask={handleOpenTask}
        />
      )}
      {activePage === 'drives' && (
        <DrivesPage
          windowHours={windowHours}
          refreshTick={refreshTick}
          onOpenCycle={handleOpenCycle}
        />
      )}
      {activePage === 'tasks' && (
        <TasksPage
          refreshTick={refreshTick}
          selectedTaskId={selectedTaskId}
          onSelectTask={setSelectedTaskId}
          onOpenCycle={handleOpenCycle}
        />
      )}
      {activePage === 'memory' && <MemoryPage refreshTick={refreshTick} />}
      {activePage === 'world' && <WorldPage refreshTick={refreshTick} />}
      {activePage === 'self-model' && <SelfModelPage refreshTick={refreshTick} />}
      {activePage === 'governance' && (
        <GovernancePage
          windowHours={windowHours}
          refreshTick={refreshTick}
          principal={principal}
          onOpenTask={handleOpenTask}
        />
      )}
      {activePage === 'config' && <ConfigPage refreshTick={refreshTick} />}
      {activePage === 'logs' && (
        <LogsPage
          windowHours={windowHours}
          refreshTick={refreshTick}
          onOpenCycle={handleOpenCycle}
          onOpenTask={handleOpenTask}
        />
      )}
    </Layout>
  );
}

export default App;
