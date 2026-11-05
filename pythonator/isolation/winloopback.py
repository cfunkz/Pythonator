# ruff: noqa: E501  (the C# keeps one statement per line)
"""Windows: the loopback permit that lets this PC's browser reach a sandboxed server.

Windows blocks loopback connections *into* an AppContainer with a Windows Filtering Platform filter
("AppContainerLoopback") in a sublayer that firewall rules can't override, and ``CheckNetIsolation LoopbackExempt -is``
lifts it only while that command keeps running. So *Make reachable* also adds one persistent permit into the
sublayer of that block, weighted just above it, and scoped as tightly as the firewall rule: that one AppContainer,
a loopback connection from ``127.0.0.1``, and the listed ports. *Undo* removes it again (found by its name).

The C# below is compiled by PowerShell's Add-Type inside the elevated script (C# 5, 64-bit Windows). ``Run`` lists
the filters of the inbound layer, removes ours, finds each sublayer with a filter that blocks loopback, and adds
the permit there. It prints "ok:<permits added>" or "error:<reason>".
"""

from __future__ import annotations

EXIT_WFP = 5  # the elevated script's exit code when the permit was refused

_WFP = r"""
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class PythonatorWfp {
  [StructLayout(LayoutKind.Sequential)] struct V { public uint T; public IntPtr P; }
  [StructLayout(LayoutKind.Sequential)] struct C { public Guid K; public uint M; public V V; }
  [StructLayout(LayoutKind.Explicit, Size = 200)] struct F {
    [FieldOffset(16)] public IntPtr Name; [FieldOffset(24)] public IntPtr Desc; [FieldOffset(32)] public uint Flags;
    [FieldOffset(64)] public Guid Layer; [FieldOffset(80)] public Guid Sub; [FieldOffset(96)] public V Weight;
    [FieldOffset(112)] public uint N; [FieldOffset(120)] public IntPtr Conds; [FieldOffset(128)] public uint Action;
    [FieldOffset(176)] public ulong Id; [FieldOffset(184)] public V Eff; }
  const string D = "fwpuclnt.dll";
  [DllImport(D, CharSet = CharSet.Unicode)] static extern uint FwpmEngineOpen0(string s, uint a, IntPtr i, IntPtr x, out IntPtr h);
  [DllImport(D)] static extern uint FwpmEngineClose0(IntPtr h);
  [DllImport(D)] static extern uint FwpmTransactionBegin0(IntPtr h, uint f);
  [DllImport(D)] static extern uint FwpmTransactionCommit0(IntPtr h);
  [DllImport(D)] static extern uint FwpmTransactionAbort0(IntPtr h);
  [DllImport(D)] static extern uint FwpmFilterAdd0(IntPtr h, ref F f, IntPtr sd, out ulong id);
  [DllImport(D)] static extern uint FwpmFilterDeleteById0(IntPtr h, ulong id);
  [DllImport(D)] static extern uint FwpmFilterCreateEnumHandle0(IntPtr h, IntPtr t, out IntPtr e);
  [DllImport(D)] static extern uint FwpmFilterEnum0(IntPtr h, IntPtr e, uint n, out IntPtr p, out uint got);
  [DllImport(D)] static extern uint FwpmFilterDestroyEnumHandle0(IntPtr h, IntPtr e);
  [DllImport(D)] static extern void FwpmFreeMemory0(ref IntPtr p);
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode)] static extern bool ConvertStringSidToSidW(string s, out IntPtr sid);
  [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr p);
  static readonly Guid RecvV4 = new Guid("e1cd9fe7-f4b5-4273-96c0-592e487b8650");
  static readonly Guid Package = new Guid("71bc78fa-f17c-4997-a602-6abb261f351c");
  static readonly Guid Flags = new Guid("632ce23b-5167-435c-86d7-e903684aa80c");
  static readonly Guid Remote = new Guid("b235ae9a-1d64-49b8-a44c-5ff3d9095045");
  static readonly Guid LocalPort = new Guid("0c1ba1af-5765-453f-af22-a8f791ac775b");
  const uint Block = 0x1001, Permit = 0x1002, Uint16 = 2, Uint32 = 3, Uint64 = 4, Sid = 13;
  public class Info { public ulong Id, Weight; public Guid Sub; public string Name; public bool IsBlock, Loopback; }

  public static List<Info> Inbound(IntPtr h) {
    var all = new List<Info>(); IntPtr e;
    uint r = FwpmFilterCreateEnumHandle0(h, IntPtr.Zero, out e);
    if (r != 0) throw new Exception("listing filters: 0x" + r.ToString("X8"));
    try {
      while (true) {
        IntPtr p; uint got;
        r = FwpmFilterEnum0(h, e, 512, out p, out got);
        if (r != 0) throw new Exception("listing filters: 0x" + r.ToString("X8"));
        try {
          for (int i = 0; i < got; i++) {
            var f = (F)Marshal.PtrToStructure(Marshal.ReadIntPtr(p, i * IntPtr.Size), typeof(F));
            if (f.Layer != RecvV4) continue;
            var x = new Info { Id = f.Id, Sub = f.Sub, IsBlock = f.Action == Block,
                               Name = f.Name == IntPtr.Zero ? "" : Marshal.PtrToStringUni(f.Name) };
            if (f.Eff.T == Uint64 && f.Eff.P != IntPtr.Zero) x.Weight = (ulong)Marshal.ReadInt64(f.Eff.P);
            for (int j = 0; j < f.N; j++) {
              var c = (C)Marshal.PtrToStructure(new IntPtr(f.Conds.ToInt64() + j * 40), typeof(C));
              if (c.K == Flags && (c.M == 6 || c.M == 7) && c.V.T == Uint32 && (c.V.P.ToInt64() & 1) != 0) x.Loopback = true;
            }
            all.Add(x);
          }
        } finally { FwpmFreeMemory0(ref p); }
        if (got < 512) return all;
      }
    } finally { FwpmFilterDestroyEnumHandle0(h, e); }
  }

  public static string Run(string sid, string name, int[] ports, bool undo) {
    if (IntPtr.Size != 8) return "error:needs 64-bit Windows";
    IntPtr h, psid = IntPtr.Zero, conds = IntPtr.Zero, weight = IntPtr.Zero, text = IntPtr.Zero, about = IntPtr.Zero;
    uint r = FwpmEngineOpen0(null, 10, IntPtr.Zero, IntPtr.Zero, out h);
    if (r != 0) return "error:opening the filtering engine: 0x" + r.ToString("X8");
    bool open = false;
    try {
      var filters = Inbound(h);
      var top = new Dictionary<Guid, ulong>();
      foreach (var x in filters) if (x.IsBlock && x.Loopback) top[x.Sub] = 0;
      foreach (var x in filters) if (x.IsBlock && top.ContainsKey(x.Sub) && x.Weight > top[x.Sub]) top[x.Sub] = x.Weight;
      if ((r = FwpmTransactionBegin0(h, 0)) != 0) return "error:starting a transaction: 0x" + r.ToString("X8");
      open = true;
      foreach (var x in filters)
        if (x.Name == name && (r = FwpmFilterDeleteById0(h, x.Id)) != 0) return "error:removing the old permit: 0x" + r.ToString("X8");
      int added = 0;
      if (!undo && top.Count > 0) {
        if (!ConvertStringSidToSidW(sid, out psid)) return "error:not a SID";
        int n = 3 + ports.Length;
        var all = new C[n];
        all[0] = new C { K = Package, M = 0, V = new V { T = Sid, P = psid } };
        all[1] = new C { K = Flags, M = 6, V = new V { T = Uint32, P = new IntPtr(1) } };
        all[2] = new C { K = Remote, M = 0, V = new V { T = Uint32, P = new IntPtr(0x7F000001) } };
        for (int i = 0; i < ports.Length; i++) all[3 + i] = new C { K = LocalPort, M = 0, V = new V { T = Uint16, P = new IntPtr(ports[i]) } };
        conds = Marshal.AllocHGlobal(40 * n);
        for (int i = 0; i < n; i++) Marshal.StructureToPtr(all[i], new IntPtr(conds.ToInt64() + i * 40), false);
        weight = Marshal.AllocHGlobal(8);
        text = Marshal.StringToHGlobalUni(name);
        about = Marshal.StringToHGlobalUni("Lets programs on this PC reach this Pythonator sandbox on 127.0.0.1.");
        foreach (var sub in top) {
          Marshal.WriteInt64(weight, (long)(sub.Value == ulong.MaxValue ? sub.Value : sub.Value + 1));
          var f = new F { Name = text, Desc = about, Flags = 1, Layer = RecvV4, Sub = sub.Key,
                          Weight = new V { T = Uint64, P = weight }, N = (uint)n, Conds = conds, Action = Permit };
          ulong id;
          if ((r = FwpmFilterAdd0(h, ref f, IntPtr.Zero, out id)) != 0) return "error:adding the permit: 0x" + r.ToString("X8");
          added++;
        }
      }
      if ((r = FwpmTransactionCommit0(h)) != 0) return "error:saving: 0x" + r.ToString("X8");
      open = false;
      return "ok:" + added;
    } catch (Exception ex) {
      return "error:" + ex.Message;
    } finally {
      if (open) FwpmTransactionAbort0(h);
      FwpmEngineClose0(h);
      if (psid != IntPtr.Zero) LocalFree(psid);
      foreach (var m in new[] { conds, weight, text, about }) if (m != IntPtr.Zero) Marshal.FreeHGlobal(m);
    }
  }
}
"""


def permit_name(container: str) -> str:
    return f"Pythonator {container} (this PC, loopback)"


def script_lines(sid: str, container: str, listed: str, *, undo: bool) -> list[str]:
    """PowerShell that compiles the C# and applies (or with ``undo`` removes) the permit. ``sid``, ``container``
    and ``listed`` (digits and commas) are validated by the caller."""
    source = "\n".join(line.strip() for line in _WFP.splitlines() if line.strip())  # a shorter command line
    return [
        "try { Add-Type -TypeDefinition @'\n" + source + "\n'@ -Language CSharp } catch "
        f"{{ [Console]::Error.WriteLine($_.Exception.Message); exit {EXIT_WFP} }}",
        f"$wfp = [PythonatorWfp]::Run('{sid}', '{permit_name(container)}', [int[]]@({listed}), "
        f"${'true' if undo else 'false'})",
        f"if (-not $wfp.StartsWith('ok')) {{ [Console]::Error.WriteLine($wfp); exit {EXIT_WFP} }}",
    ]
