// RMon - teto de memoria por processo para o RM.Host.JobRunner (Windows Job Object).
//
// Servico do Windows que poe cada RM.Host.JobRunner (filho do RM.Host.Service) num
// Job Object proprio. Com o modo "aplicar", o Windows nega a alocacao que passaria
// do teto (JOB_OBJECT_LIMIT_PROCESS_MEMORY): o job do RM falha por falta de memoria
// e o servidor segue de pe - sem depender de coleta, rede ou WinRM. Com o modo
// "registrar" (o padrao) nada e bloqueado: um limite so de aviso (notification
// limit) grava no Event Log quando o processo passaria do teto, para calibrar.
//
// Compilado NO SERVIDOR pelo deploy/windows/instalar-limite-jobrunner.ps1 com o
// csc.exe do .NET Framework 4 (C# 5: sem interpolacao de string nem ?.). Fonte
// ASCII: os acentos das mensagens vao como \u00xx.
//
// Configuracao: HKLM\SOFTWARE\RMon\LimiteJobRunner (Modo, TetoMB, Processo, Pais,
// IntervaloMs), relida a cada varredura - mudar o modo nao exige reiniciar.
// Estado para o RMon: %ProgramData%\RMon\LimiteJobRunner\estado.json (a cada 30 s).
// Eventos: log Application, origem RMon-LimiteJobRunner (ids na docs/OPERACAO.md).
//
// Teste local, sem instalar (nao precisa de administrador):
//   RMonLimiteJobRunner.exe /console /processo:Comedor /pais:* /teto:300 /modo:aplicar
//                           /estado:C:\temp\lj /segundos:60
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;

[assembly: System.Reflection.AssemblyTitle("RMon - teto de memoria do RM.Host.JobRunner")]
[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]

namespace RMon.LimiteJobRunner
{
    internal static class N
    {
        public const uint PROCESS_TERMINATE = 0x0001;
        public const uint PROCESS_VM_READ = 0x0010;
        public const uint PROCESS_SET_QUOTA = 0x0100;
        public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        public const uint SYNCHRONIZE = 0x00100000;

        public const uint LIMIT_PROCESS_MEMORY = 0x00000100;
        public const uint LIMIT_JOB_MEMORY = 0x00000200;
        public const uint LIMIT_BREAKAWAY_OK = 0x00000800;

        public const int ClassAssociateCompletionPort = 7;
        public const int ClassExtendedLimit = 9;
        public const int ClassNotificationLimit = 12;

        public const uint MSG_ACTIVE_PROCESS_ZERO = 4;
        public const uint MSG_EXIT_PROCESS = 7;
        public const uint MSG_ABNORMAL_EXIT_PROCESS = 8;
        public const uint MSG_PROCESS_MEMORY_LIMIT = 9;
        public const uint MSG_JOB_MEMORY_LIMIT = 10;
        public const uint MSG_NOTIFICATION_LIMIT = 11;

        [StructLayout(LayoutKind.Sequential)]
        public struct BASIC_LIMIT
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct IO_COUNTERS
        {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
            public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct EXTENDED_LIMIT
        {
            public BASIC_LIMIT Basic;
            public IO_COUNTERS Io;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct NOTIFICATION_LIMIT
        {
            public ulong IoReadBytesLimit;
            public ulong IoWriteBytesLimit;
            public long PerJobUserTimeLimit;
            public ulong JobMemoryLimit;
            public int RateControlTolerance;
            public int RateControlToleranceInterval;
            public uint LimitFlags;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct ASSOCIATE_PORT
        {
            public IntPtr CompletionKey;
            public IntPtr CompletionPort;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct MEMORY_COUNTERS_EX
        {
            public uint cb;
            public uint PageFaultCount;
            public UIntPtr PeakWorkingSetSize, WorkingSetSize;
            public UIntPtr QuotaPeakPagedPoolUsage, QuotaPagedPoolUsage;
            public UIntPtr QuotaPeakNonPagedPoolUsage, QuotaNonPagedPoolUsage;
            public UIntPtr PagefileUsage, PeakPagefileUsage, PrivateUsage;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct PROCESSENTRY32
        {
            public uint dwSize;
            public uint cntUsage;
            public uint th32ProcessID;
            public IntPtr th32DefaultHeapID;
            public uint th32ModuleID;
            public uint cntThreads;
            public uint th32ParentProcessID;
            public int pcPriClassBase;
            public uint dwFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string szExeFile;
        }

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern IntPtr CreateJobObject(IntPtr attr, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetInformationJobObject(IntPtr job, int cls, ref EXTENDED_LIMIT info, uint len);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetInformationJobObject(IntPtr job, int cls, ref NOTIFICATION_LIMIT info, uint len);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetInformationJobObject(IntPtr job, int cls, ref ASSOCIATE_PORT info, uint len);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool QueryInformationJobObject(IntPtr job, int cls, out EXTENDED_LIMIT info, uint len, IntPtr ret);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr CreateIoCompletionPort(IntPtr file, IntPtr existing, UIntPtr key, uint threads);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetQueuedCompletionStatus(IntPtr port, out uint bytes, out UIntPtr key, out IntPtr overlapped, uint ms);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool PostQueuedCompletionStatus(IntPtr port, uint bytes, UIntPtr key, IntPtr overlapped);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr h);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint WaitForSingleObject(IntPtr h, uint ms);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool GetExitCodeProcess(IntPtr h, out uint code);
        [DllImport("kernel32.dll", SetLastError = true, EntryPoint = "K32GetProcessMemoryInfo")]
        public static extern bool GetProcessMemoryInfo(IntPtr h, out MEMORY_COUNTERS_EX c, uint cb);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "Process32FirstW")]
        public static extern bool Process32First(IntPtr snap, ref PROCESSENTRY32 e);
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode, EntryPoint = "Process32NextW")]
        public static extern bool Process32Next(IntPtr snap, ref PROCESSENTRY32 e);
    }

    internal sealed class Config
    {
        public const string Chave = @"SOFTWARE\RMon\LimiteJobRunner";
        public string Modo = "registrar";
        public int TetoMB = 6144;
        public string Processo = "RM.Host.JobRunner";
        public string[] Pais = new string[] { "RM.Host.Service*" };
        public int IntervaloMs = 2000;
        public string Problema;

        public bool Aplicar { get { return Modo == "aplicar"; } }
        public string Assinatura() { return Modo + "|" + TetoMB; }

        public static Config Ler(Dictionary<string, string> args)
        {
            Config c = new Config();
            Dictionary<string, string> v = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            try
            {
                using (RegistryKey k = Registry.LocalMachine.OpenSubKey(Chave))
                {
                    if (k != null)
                        foreach (string n in k.GetValueNames())
                        {
                            object o = k.GetValue(n);
                            if (o != null) v[n] = Convert.ToString(o);
                        }
                }
            }
            catch (Exception) { }
            foreach (KeyValuePair<string, string> a in args) v[a.Key] = a.Value;
            string s;
            if (v.TryGetValue("modo", out s))
            {
                s = s.Trim().ToLowerInvariant();
                if (s == "registrar" || s == "aplicar") c.Modo = s;
                else c.Problema = "Modo '" + s + "' desconhecido: usando registrar";
            }
            int i;
            if (v.TryGetValue("tetomb", out s) || v.TryGetValue("teto", out s))
            {
                if (int.TryParse(s, out i) && i >= 256) c.TetoMB = i;
                else c.Problema = "TetoMB '" + s + "' inv\u00e1lido (m\u00ednimo 256): usando " + c.TetoMB;
            }
            if (v.TryGetValue("processo", out s) && s.Trim().Length > 0) c.Processo = s.Trim();
            if (v.TryGetValue("pais", out s) && s.Trim().Length > 0)
            {
                List<string> l = new List<string>();
                foreach (string p in s.Split(new char[] { ',', ';' }, StringSplitOptions.RemoveEmptyEntries))
                    if (p.Trim().Length > 0) l.Add(p.Trim());
                if (l.Count > 0) c.Pais = l.ToArray();
            }
            if ((v.TryGetValue("intervaloms", out s) || v.TryGetValue("intervalo", out s)) && int.TryParse(s, out i))
                c.IntervaloMs = Math.Max(500, Math.Min(60000, i));
            return c;
        }
    }

    internal sealed class Registro
    {
        public int Pid;
        public IntPtr Processo;
        public IntPtr Job;
        public UIntPtr Chave;
        public string Pai;
        public string Erro;
        public bool Estourou;
        public long MB;
        public DateTime Visto;
    }

    internal sealed class Motor
    {
        public const string Fonte = "RMon-LimiteJobRunner";
        const long MB = 1024L * 1024L;
        const int MaxEstouros = 20;

        readonly Dictionary<string, string> args;
        readonly bool console;
        readonly string estadoDir;
        readonly object trava = new object();
        readonly Dictionary<int, Registro> regs = new Dictionary<int, Registro>();
        readonly Dictionary<ulong, Registro> porChave = new Dictionary<ulong, Registro>();
        readonly HashSet<int> ignorados = new HashSet<int>();
        readonly Dictionary<string, DateTime> errosVistos = new Dictionary<string, DateTime>();
        readonly ArrayList estouros = new ArrayList();
        IntPtr porta;
        Thread tVarre, tPorta;
        volatile bool parar;
        Config cfg;
        ulong proximaChave = 1;
        DateTime iniciado, ultimoEstado = DateTime.MinValue;
        bool sujo;
        int processos;
        string ultimoErro;
        bool fonteOk;

        public Motor(Dictionary<string, string> args, bool console)
        {
            this.args = args;
            this.console = console;
            string d;
            estadoDir = args.TryGetValue("estado", out d) ? d
                : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), @"RMon\LimiteJobRunner");
        }

        // ------------------------------------------------------------ registro

        void Log(EventLogEntryType tipo, int id, string msg)
        {
            if (console) Console.WriteLine("{0:HH:mm:ss} [{1} {2}] {3}", DateTime.Now, tipo, id, msg);
            if (!fonteOk) return;
            try { EventLog.WriteEntry(Fonte, msg, tipo, id); } catch (Exception) { }
        }

        // erro repetido vai para o Event Log no maximo a cada 10 min
        void Erro(int id, string msg)
        {
            ultimoErro = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + msg;
            sujo = true;
            lock (errosVistos)
            {
                DateTime t;
                if (errosVistos.TryGetValue(msg, out t) && DateTime.UtcNow - t < TimeSpan.FromMinutes(10)) return;
                errosVistos[msg] = DateTime.UtcNow;
            }
            Log(EventLogEntryType.Error, id, msg);
        }

        static string Txt(Config c)
        {
            return c.Aplicar
                ? "aplicar (o Windows nega a mem\u00f3ria acima do teto)"
                : "s\u00f3 registra (nada \u00e9 bloqueado; o aviso vai para o Event Log)";
        }

        // ------------------------------------------------------------ ciclo de vida

        public void Iniciar()
        {
            try { fonteOk = EventLog.SourceExists(Fonte); } catch (Exception) { fonteOk = false; }
            iniciado = DateTime.Now;
            cfg = Config.Ler(args);
            CarregarEstouros();
            porta = N.CreateIoCompletionPort(new IntPtr(-1), IntPtr.Zero, UIntPtr.Zero, 1);
            if (porta == IntPtr.Zero) throw new Exception("CreateIoCompletionPort falhou: " + Marshal.GetLastWin32Error());
            Log(EventLogEntryType.Information, 1000,
                "Teto de mem\u00f3ria do " + cfg.Processo + " iniciado. Modo: " + Txt(cfg) + ". Teto: " + cfg.TetoMB +
                " MB por processo. Pais aceitos: " + string.Join(", ", cfg.Pais) + ". Varredura a cada " + cfg.IntervaloMs + " ms.");
            if (cfg.Problema != null) Erro(1098, "Configura\u00e7\u00e3o: " + cfg.Problema);
            tPorta = new Thread(LacoPorta); tPorta.IsBackground = true; tPorta.Name = "porta"; tPorta.Start();
            tVarre = new Thread(LacoVarre); tVarre.IsBackground = true; tVarre.Name = "varredura"; tVarre.Start();
        }

        // liberar = tirar o teto de todos (parada pedida). Num desligamento do
        // Windows nao precisa: os processos morrem junto.
        public void Parar(bool liberar)
        {
            parar = true;
            N.PostQueuedCompletionStatus(porta, 0, UIntPtr.Zero, IntPtr.Zero);
            if (tVarre != null) tVarre.Join(5000);
            if (tPorta != null) tPorta.Join(5000);
            int n = 0;
            lock (trava)
            {
                foreach (Registro r in regs.Values)
                {
                    if (liberar && r.Job != IntPtr.Zero && Liberar(r)) n++;
                    Fechar(r);
                }
                regs.Clear();
                porChave.Clear();
            }
            EscreverEstado(false);
            if (porta != IntPtr.Zero) N.CloseHandle(porta);
            Log(EventLogEntryType.Information, 1001, liberar
                ? "Teto de mem\u00f3ria parado: " + n + " processo(s) ficaram sem teto at\u00e9 o servi\u00e7o voltar."
                : "Teto de mem\u00f3ria parado (desligamento do Windows).");
        }

        void LacoVarre()
        {
            while (!parar)
            {
                try { Varrer(); }
                catch (Exception e) { Erro(1099, "Falha na varredura: " + e.GetType().Name + ": " + e.Message); }
                int espera = cfg != null ? cfg.IntervaloMs : 2000;
                for (int t = 0; t < espera && !parar; t += 100) Thread.Sleep(100);
            }
        }

        void LacoPorta()
        {
            while (!parar)
            {
                uint msg; UIntPtr chave; IntPtr ov;
                bool ok = N.GetQueuedCompletionStatus(porta, out msg, out chave, out ov, 1000);
                if (parar) break;
                if (!ok) continue;
                try
                {
                    Registro r;
                    lock (trava) { porChave.TryGetValue(chave.ToUInt64(), out r); }
                    if (r == null) continue;
                    if (msg == N.MSG_PROCESS_MEMORY_LIMIT || msg == N.MSG_JOB_MEMORY_LIMIT) Estouro(r, true);
                    else if (msg == N.MSG_NOTIFICATION_LIMIT) Estouro(r, false);
                    else if (msg == N.MSG_ACTIVE_PROCESS_ZERO) Encerrar(r);
                    else if ((msg == N.MSG_EXIT_PROCESS || msg == N.MSG_ABNORMAL_EXIT_PROCESS) && ov.ToInt64() == r.Pid) Encerrar(r);
                }
                catch (Exception e) { Erro(1099, "Falha ao tratar aviso do Job Object: " + e.GetType().Name + ": " + e.Message); }
            }
        }

        // ------------------------------------------------------------ varredura

        static string SemExe(string nome)
        {
            return nome.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? nome.Substring(0, nome.Length - 4) : nome;
        }

        public static bool Casa(string nome, string padrao)
        {
            string re = "^" + Regex.Escape(padrao).Replace(@"\*", ".*").Replace(@"\?", ".") + "$";
            return Regex.IsMatch(nome, re, RegexOptions.IgnoreCase);
        }

        static List<N.PROCESSENTRY32> Instantaneo()
        {
            List<N.PROCESSENTRY32> l = new List<N.PROCESSENTRY32>();
            IntPtr s = N.CreateToolhelp32Snapshot(2, 0);
            if (s == new IntPtr(-1)) throw new Exception("CreateToolhelp32Snapshot falhou: " + Marshal.GetLastWin32Error());
            try
            {
                N.PROCESSENTRY32 e = new N.PROCESSENTRY32();
                e.dwSize = (uint)Marshal.SizeOf(typeof(N.PROCESSENTRY32));
                if (N.Process32First(s, ref e))
                    do { l.Add(e); } while (N.Process32Next(s, ref e));
            }
            finally { N.CloseHandle(s); }
            return l;
        }

        void Varrer()
        {
            Config novo = Config.Ler(args);
            bool mudou = novo.Assinatura() != cfg.Assinatura();
            if (mudou)
            {
                Log(EventLogEntryType.Warning, 1002, "Configura\u00e7\u00e3o mudou: modo " + Txt(novo) + ", teto " + novo.TetoMB +
                    " MB (antes: " + cfg.Modo + ", " + cfg.TetoMB + " MB). Aplicando a todos os processos sob o teto.");
                if (novo.Problema != null) Erro(1098, "Configura\u00e7\u00e3o: " + novo.Problema);
            }
            cfg = novo;

            List<N.PROCESSENTRY32> todos = Instantaneo();
            Dictionary<int, string> nomes = new Dictionary<int, string>();
            foreach (N.PROCESSENTRY32 p in todos) nomes[(int)p.th32ProcessID] = SemExe(p.szExeFile);
            HashSet<int> vivos = new HashSet<int>();
            List<string> novos = new List<string>();
            foreach (N.PROCESSENTRY32 p in todos)
            {
                if (!string.Equals(SemExe(p.szExeFile), cfg.Processo, StringComparison.OrdinalIgnoreCase)) continue;
                int pid = (int)p.th32ProcessID;
                vivos.Add(pid);
                lock (trava) { if (regs.ContainsKey(pid)) continue; }
                if (ignorados.Contains(pid)) continue;
                string pai;
                nomes.TryGetValue((int)p.th32ParentProcessID, out pai);
                if (pai != null && !PaiAceito(pai))
                {
                    ignorados.Add(pid);
                    Log(EventLogEntryType.Information, 1011, cfg.Processo + " PID " + pid + " \u00e9 filho de " + pai +
                        ", fora dos pais aceitos (" + string.Join(", ", cfg.Pais) + "): ficou sem teto.");
                    continue;
                }
                string r = Proteger(pid, pai);
                if (r != null) novos.Add(r);
            }
            ignorados.RemoveWhere(delegate (int x) { return !vivos.Contains(x); });
            processos = vivos.Count;
            if (novos.Count > 0)
            {
                Log(EventLogEntryType.Information, 1010, novos.Count + " processo(s) " + cfg.Processo + " sob o teto de " +
                    cfg.TetoMB + " MB, modo " + Txt(cfg) + ": " + string.Join("; ", novos.ToArray()) + ".");
                sujo = true;
            }

            List<Registro> fim = new List<Registro>();
            lock (trava)
            {
                foreach (Registro r in regs.Values)
                {
                    if (mudou && r.Job != IntPtr.Zero)
                    {
                        r.Estourou = false;
                        string e = Limites(r.Job);
                        if (e != null) Erro(1091, "PID " + r.Pid + ": n\u00e3o consegui mudar o teto: " + e);
                    }
                    r.MB = MemoriaMB(r.Processo);
                    // rede de seguranca: aviso de saida perdido na porta
                    if (N.WaitForSingleObject(r.Processo, 0) == 0) fim.Add(r);
                }
            }
            foreach (Registro r in fim) Encerrar(r);
            if (mudou || sujo || DateTime.UtcNow - ultimoEstado >= TimeSpan.FromSeconds(30)) EscreverEstado(true);
        }

        bool PaiAceito(string pai)
        {
            foreach (string p in cfg.Pais) if (Casa(pai, p)) return true;
            return false;
        }

        static long MemoriaMB(IntPtr h)
        {
            N.MEMORY_COUNTERS_EX c;
            if (!N.GetProcessMemoryInfo(h, out c, (uint)Marshal.SizeOf(typeof(N.MEMORY_COUNTERS_EX)))) return -1;
            return (long)(c.PrivateUsage.ToUInt64() / (ulong)MB);
        }

        // Teto conforme o modo. BREAKAWAY_OK sempre: se o RM criar filho pedindo
        // para sair do job, continua funcionando como antes de existir o job.
        string Limites(IntPtr job)
        {
            N.EXTENDED_LIMIT ext = new N.EXTENDED_LIMIT();
            ext.Basic.LimitFlags = N.LIMIT_BREAKAWAY_OK;
            N.NOTIFICATION_LIMIT av = new N.NOTIFICATION_LIMIT();
            av.RateControlTolerance = 1;
            av.RateControlToleranceInterval = 1;
            ulong teto = (ulong)cfg.TetoMB * (ulong)MB;
            if (cfg.Aplicar)
            {
                ext.Basic.LimitFlags |= N.LIMIT_PROCESS_MEMORY;
                ext.ProcessMemoryLimit = new UIntPtr(teto);
            }
            else
            {
                av.LimitFlags = N.LIMIT_JOB_MEMORY;
                av.JobMemoryLimit = teto;
            }
            if (!N.SetInformationJobObject(job, N.ClassExtendedLimit, ref ext, (uint)Marshal.SizeOf(typeof(N.EXTENDED_LIMIT))))
                return "SetInformationJobObject(limite) erro " + Marshal.GetLastWin32Error();
            if (!N.SetInformationJobObject(job, N.ClassNotificationLimit, ref av, (uint)Marshal.SizeOf(typeof(N.NOTIFICATION_LIMIT))))
                return "SetInformationJobObject(aviso) erro " + Marshal.GetLastWin32Error();
            return null;
        }

        static bool Liberar(Registro r)
        {
            N.EXTENDED_LIMIT ext = new N.EXTENDED_LIMIT();
            ext.Basic.LimitFlags = N.LIMIT_BREAKAWAY_OK;
            N.NOTIFICATION_LIMIT av = new N.NOTIFICATION_LIMIT();
            av.RateControlTolerance = 1;
            av.RateControlToleranceInterval = 1;
            bool a = N.SetInformationJobObject(r.Job, N.ClassExtendedLimit, ref ext, (uint)Marshal.SizeOf(typeof(N.EXTENDED_LIMIT)));
            bool b = N.SetInformationJobObject(r.Job, N.ClassNotificationLimit, ref av, (uint)Marshal.SizeOf(typeof(N.NOTIFICATION_LIMIT)));
            return a && b;
        }

        // Devolve o texto do processo protegido, ou null se nao deu.
        string Proteger(int pid, string pai)
        {
            IntPtr h = N.OpenProcess(N.PROCESS_SET_QUOTA | N.PROCESS_TERMINATE | N.PROCESS_QUERY_LIMITED_INFORMATION |
                                     N.PROCESS_VM_READ | N.SYNCHRONIZE, false, pid);
            if (h == IntPtr.Zero)
            {
                int e = Marshal.GetLastWin32Error();
                if (e != 87) Erro(1090, cfg.Processo + " PID " + pid + ": n\u00e3o consegui abrir o processo (erro " + e + "): ficou sem teto.");
                return null;  // 87: terminou entre a lista e a abertura
            }
            Registro r = new Registro();
            r.Pid = pid; r.Processo = h; r.Pai = pai ?? "?"; r.Visto = DateTime.Now;
            r.Chave = new UIntPtr(proximaChave++);
            bool jaEmJob;
            N.IsProcessInJob(h, IntPtr.Zero, out jaEmJob);
            string erro = null;
            IntPtr job = N.CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) erro = "CreateJobObject erro " + Marshal.GetLastWin32Error();
            if (erro == null)
            {
                N.ASSOCIATE_PORT ap = new N.ASSOCIATE_PORT();
                ap.CompletionKey = new IntPtr((long)r.Chave.ToUInt64());
                ap.CompletionPort = porta;
                if (!N.SetInformationJobObject(job, N.ClassAssociateCompletionPort, ref ap, (uint)Marshal.SizeOf(typeof(N.ASSOCIATE_PORT))))
                    erro = "associar a porta erro " + Marshal.GetLastWin32Error();
            }
            if (erro == null) erro = Limites(job);
            if (erro == null && !N.AssignProcessToJobObject(job, h))
                erro = "AssignProcessToJobObject erro " + Marshal.GetLastWin32Error() + (jaEmJob ? " (o processo j\u00e1 estava em outro job)" : "");
            if (erro != null)
            {
                if (job != IntPtr.Zero) N.CloseHandle(job);
                r.Erro = erro;
                Erro(1090, cfg.Processo + " PID " + pid + " (filho de " + r.Pai + "): n\u00e3o consegui p\u00f4r sob o teto: " + erro + ".");
            }
            else r.Job = job;
            r.MB = MemoriaMB(h);
            lock (trava)
            {
                regs[pid] = r;
                porChave[r.Chave.ToUInt64()] = r;
            }
            return erro == null ? "PID " + pid + " (" + r.Pai + (jaEmJob ? ", j\u00e1 estava num job: aninhado" : "") + ")" : null;
        }

        void Estouro(Registro r, bool aplicado)
        {
            long mb = MemoriaMB(r.Processo);
            lock (trava)
            {
                if (r.Estourou) return;
                r.Estourou = true;
                Hashtable e = new Hashtable();
                e["pid"] = r.Pid; e["pai"] = r.Pai; e["mb"] = mb; e["teto_mb"] = cfg.TetoMB;
                e["modo"] = aplicado ? "aplicar" : "registrar";
                e["quando"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
                e["quando_epoch"] = Epoch(DateTime.UtcNow);
                estouros.Add(e);
                while (estouros.Count > MaxEstouros) estouros.RemoveAt(0);
                sujo = true;
            }
            if (aplicado)
                Log(EventLogEntryType.Error, 1021, cfg.Processo + " PID " + r.Pid + " (filho de " + r.Pai + ") bateu no teto de " +
                    cfg.TetoMB + " MB (est\u00e1 com " + mb + " MB) e o Windows negou a mem\u00f3ria pedida. O job em execu\u00e7\u00e3o " +
                    "deve falhar por falta de mem\u00f3ria e o RM.Host rep\u00f5e o processo; o servidor segue de p\u00e9.");
            else
                Log(EventLogEntryType.Warning, 1020, cfg.Processo + " PID " + r.Pid + " (filho de " + r.Pai + ") passou do teto de " +
                    cfg.TetoMB + " MB: est\u00e1 com " + mb + " MB. Modo s\u00f3 registra: nada foi bloqueado. No modo aplicar, " +
                    "o Windows teria negado a mem\u00f3ria e o job falharia por falta de mem\u00f3ria.");
        }

        void Encerrar(Registro r)
        {
            lock (trava)
            {
                Registro atual;
                if (!regs.TryGetValue(r.Pid, out atual) || !object.ReferenceEquals(atual, r)) return;
                regs.Remove(r.Pid);
                porChave.Remove(r.Chave.ToUInt64());
            }
            long pico = -1;
            if (r.Job != IntPtr.Zero)
            {
                N.EXTENDED_LIMIT ext;
                if (N.QueryInformationJobObject(r.Job, N.ClassExtendedLimit, out ext, (uint)Marshal.SizeOf(typeof(N.EXTENDED_LIMIT)), IntPtr.Zero))
                    pico = (long)(ext.PeakProcessMemoryUsed.ToUInt64() / (ulong)MB);
            }
            uint codigo;
            string saida = N.GetExitCodeProcess(r.Processo, out codigo) ? "0x" + codigo.ToString("X8") : "?";
            Fechar(r);
            lock (trava)
            {
                foreach (Hashtable e in estouros)
                    if (Convert.ToInt32(e["pid"]) == r.Pid && !e.ContainsKey("fim"))
                    {
                        e["fim"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
                        e["pico_mb"] = pico;
                        e["saida"] = saida;
                    }
                sujo = true;
            }
            if (r.Estourou || pico >= cfg.TetoMB / 2)
                Log(r.Estourou ? EventLogEntryType.Warning : EventLogEntryType.Information, 1030,
                    cfg.Processo + " PID " + r.Pid + " terminou (c\u00f3digo de sa\u00edda " + saida + "). Maior mem\u00f3ria usada: " +
                    pico + " MB, teto de " + cfg.TetoMB + " MB.");
        }

        static void Fechar(Registro r)
        {
            if (r.Job != IntPtr.Zero) { N.CloseHandle(r.Job); r.Job = IntPtr.Zero; }
            if (r.Processo != IntPtr.Zero) { N.CloseHandle(r.Processo); r.Processo = IntPtr.Zero; }
        }

        // ------------------------------------------------------------ estado para o RMon

        static long Epoch(DateTime utc) { return (long)(utc - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds; }

        string Arquivo { get { return Path.Combine(estadoDir, "estado.json"); } }

        void CarregarEstouros()
        {
            try
            {
                if (!File.Exists(Arquivo)) return;
                Dictionary<string, object> d = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(Arquivo, Encoding.UTF8));
                object l;
                if (d != null && d.TryGetValue("estouros", out l) && l is ArrayList)
                    foreach (object o in (ArrayList)l)
                    {
                        Dictionary<string, object> x = o as Dictionary<string, object>;
                        if (x == null) continue;
                        Hashtable e = new Hashtable();
                        foreach (KeyValuePair<string, object> kv in x) e[kv.Key] = kv.Value;
                        if (!e.ContainsKey("fim")) e["fim"] = "?";  // o servico parou antes do fim
                        estouros.Add(e);
                    }
            }
            catch (Exception e) { Erro(1097, "N\u00e3o consegui ler o estado anterior (" + Arquivo + "): " + e.Message); }
        }

        void EscreverEstado(bool ativo)
        {
            Hashtable d = new Hashtable();
            lock (trava)
            {
                int sob = 0, falhas = 0; long maior = -1; int maiorPid = 0;
                foreach (Registro r in regs.Values)
                {
                    if (r.Job != IntPtr.Zero) sob++; else falhas++;
                    if (r.MB > maior) { maior = r.MB; maiorPid = r.Pid; }
                }
                d["versao"] = "1.0";
                d["ativo"] = ativo;
                d["modo"] = cfg.Modo;
                d["teto_mb"] = cfg.TetoMB;
                d["processo"] = cfg.Processo;
                d["pais"] = cfg.Pais;
                d["intervalo_ms"] = cfg.IntervaloMs;
                d["iniciado"] = iniciado.ToString("yyyy-MM-dd HH:mm:ss");
                d["atualizado"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
                d["atualizado_epoch"] = Epoch(DateTime.UtcNow);
                d["processos"] = processos;
                d["sob_teto"] = ativo ? sob : 0;
                d["falhas"] = falhas;
                d["ignorados"] = ignorados.Count;
                d["maior_mb"] = maior >= 0 ? (object)maior : null;
                d["maior_pid"] = maior >= 0 ? (object)maiorPid : null;
                d["estouros"] = new ArrayList(estouros);
                d["erro"] = ultimoErro;
                sujo = false;
            }
            ultimoEstado = DateTime.UtcNow;
            try
            {
                Directory.CreateDirectory(estadoDir);
                string tmp = Arquivo + ".tmp";
                File.WriteAllText(tmp, new JavaScriptSerializer().Serialize(d), new UTF8Encoding(false));
                if (File.Exists(Arquivo)) File.Replace(tmp, Arquivo, null);
                else File.Move(tmp, Arquivo);
            }
            catch (Exception e) { Erro(1096, "N\u00e3o consegui gravar " + Arquivo + ": " + e.Message); }
        }
    }

    internal sealed class Servico : ServiceBase
    {
        public const string Nome = "RMonLimiteJobRunner";
        Motor motor;

        public Servico()
        {
            ServiceName = Nome;
            CanStop = true;
            CanShutdown = true;
            AutoLog = false;
        }

        protected override void OnStart(string[] a)
        {
            motor = new Motor(Programa.Args(a), false);
            motor.Iniciar();
        }

        protected override void OnStop() { if (motor != null) motor.Parar(true); }
        protected override void OnShutdown() { if (motor != null) motor.Parar(false); }
    }

    internal static class Programa
    {
        // /chave:valor -> {chave: valor}; /console vira {console: ""}
        public static Dictionary<string, string> Args(string[] a)
        {
            Dictionary<string, string> d = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (string s in a)
            {
                string t = s.TrimStart('/', '-');
                int i = t.IndexOf(':');
                if (i < 0) i = t.IndexOf('=');
                if (i < 0) d[t] = "";
                else d[t.Substring(0, i)] = t.Substring(i + 1);
            }
            return d;
        }

        static int Main(string[] a)
        {
            Dictionary<string, string> args = Args(a);
            if (!args.ContainsKey("console"))
            {
                ServiceBase.Run(new Servico());
                return 0;
            }
            args.Remove("console");
            Console.OutputEncoding = new UTF8Encoding(false);
            string seg;
            int segundos = 0;
            if (args.TryGetValue("segundos", out seg)) { int.TryParse(seg, out segundos); args.Remove("segundos"); }
            Motor m = new Motor(args, true);
            m.Iniciar();
            ManualResetEvent fim = new ManualResetEvent(false);
            Console.CancelKeyPress += delegate (object o, ConsoleCancelEventArgs e) { e.Cancel = true; fim.Set(); };
            if (segundos > 0) fim.WaitOne(segundos * 1000);
            else fim.WaitOne();
            m.Parar(true);
            return 0;
        }
    }
}
