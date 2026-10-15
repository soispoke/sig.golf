/-
Copyright (c) 2023 Kim Morrison. All rights reserved.
Released under Apache 2.0 license; see LICENSE in this directory.
Authors: Kim Morrison

Modified from Lean 4.33.1 Lean/Replay.lean and the existing RP timing prototype:
adds declaration timings and streamed partial output, while preserving the
postponed constructor/recursor checks. Diagnostic only, not a certificate verifier.
-/
/- Replay profiler: same parse + Environment.replay as leanprover/comparator (c0c5a52),
   but with per-declaration kernel timings. Modes:
     replayprof replay <export.ndjson> <out.tsv>
     replayprof modmap <Module> <in.tsv> <out.tsv>   (adds the defining module of each name) -/
import Lean
import Export.Parse
open Lean

namespace RP

structure Context where
  newConstants : Std.HashMap Name ConstantInfo
  timingLog : Option IO.FS.Handle := none

structure State where
  env : Kernel.Environment
  remaining : NameSet := {}
  pending : NameSet := {}
  postponedConstructors : NameSet := {}
  postponedRecursors : NameSet := {}
  timings : Array (Name × String × Nat) := #[]
  lastReport : Nat := 0
  startNs : Nat := 0
  totalNs : Nat := 0

abbrev M := ReaderT Context <| StateRefT State IO

def isTodo (name : Name) : M Bool := do
  let r := (← get).remaining
  if r.contains name then
    modify fun s => { s with remaining := s.remaining.erase name, pending := s.pending.insert name }
    return true
  else
    return false

def addDecl (name : Name) (kind : String) (d : Declaration) : M Unit := do
  let t0 ← IO.monoNanosNow
  match (← get).env.addDeclCore 0 0 d (cancelTk? := none) with
  | .ok env => modify fun s => { s with env := env }
  | .error ex => throw <| .userError <| (← ex.toMessageData {} |>.toString)
  let t1 ← IO.monoNanosNow
  if let some log := (← read).timingLog then
    log.putStrLn s!"{(t1 - t0) / 1000}\t{kind}\t{name}"
    log.flush
  modify fun s => { s with timings := s.timings.push (name, kind, t1 - t0), totalNs := s.totalNs + (t1 - t0) }
  if t1 - t0 > 5000000000 then
    IO.println s!"  SLOW {(t1 - t0) / 1000000} ms {kind} {name}"
    (← IO.getStdout).flush
  let st ← get
  if t1 - st.lastReport > 300000000000 then
    IO.println s!"  PROGRESS t={(t1 - st.startNs) / 1000000000}s decls={st.timings.size} kernel={st.totalNs / 1000000000}s last={name}"
    (← IO.getStdout).flush
    modify fun s => { s with lastReport := t1 }

mutual
partial def replayConstant (name : Name) : M Unit := do
  if ← isTodo name then
    let some ci := (← read).newConstants[name]? | unreachable!
    replayConstants ci.getUsedConstantsAsSet
    if (← get).pending.contains name then
      try
        match ci with
        | .defnInfo   info => addDecl name "defn" (Declaration.defnDecl info)
        | .thmInfo    info =>
          if let some (.thmInfo info') := (← get).env.find? ci.name then
            if info.name == info'.name && info.type == info'.type &&
              info.levelParams == info'.levelParams && info.all == info'.all then
              return
          addDecl name "thm" (Declaration.thmDecl info)
        | .axiomInfo  info => addDecl name "axiom" (Declaration.axiomDecl info)
        | .opaqueInfo info => addDecl name "opaque" (Declaration.opaqueDecl info)
        | .inductInfo info =>
          let lparams := info.levelParams
          let nparams := info.numParams
          let all ← info.all.mapM fun n => do pure <| ((← read).newConstants[n]!)
          for o in all do
            modify fun s =>
              { s with remaining := s.remaining.erase o.name, pending := s.pending.erase o.name }
          let ctorInfo ← all.mapM fun ci => do
            pure (ci, ← ci.inductiveVal!.ctors.mapM fun n => do
              pure ((← read).newConstants[n]!))
          for (_, ctors) in ctorInfo do
            for ctor in ctors do
              replayConstants ctor.getUsedConstantsAsSet
          let types : List InductiveType := ctorInfo.map fun ⟨ci, ctors⟩ =>
            { name := ci.name, type := ci.type
              ctors := ctors.map fun ci => { name := ci.name, type := ci.type } }
          addDecl name "induct" (Declaration.inductDecl lparams nparams types false)
        | .ctorInfo info =>
          modify fun s => { s with postponedConstructors := s.postponedConstructors.insert info.name }
        | .recInfo info =>
          modify fun s => { s with postponedRecursors := s.postponedRecursors.insert info.name }
        | .quotInfo _ =>
          replayConstant `Eq
          addDecl name "quot" (Declaration.quotDecl)
        modify fun s => { s with pending := s.pending.erase name }
      catch ex =>
        throw <| .userError s!"while replaying declaration '{name}':\n{ex}"

partial def replayConstants (names : NameSet) : M Unit := do
  for n in names do replayConstant n
end

def checkPostponedConstructors : M Unit := do
  for ctor in (← get).postponedConstructors do
    match (← get).env.find? ctor, (← read).newConstants[ctor]? with
    | some (.ctorInfo info), some (.ctorInfo info') =>
      if ! (info == info') then throw <| IO.userError s!"Invalid constructor {ctor}"
    | _, _ => throw <| IO.userError s!"No such constructor {ctor}"

/--
Check that all postponed recursors are identical to those generated
when we replayed the inductives.
-/
def checkPostponedRecursors : M Unit := do
  for ctor in (← get).postponedRecursors do
    match (← get).env.find? ctor, (← read).newConstants[ctor]? with
    | some (.recInfo info), some (.recInfo info') =>
      if ! (info == info') then throw <| IO.userError s!"Invalid recursor {ctor}"
    | _, _ => throw <| IO.userError s!"No such recursor {ctor}"


def ms (a b : Nat) : String := toString ((b - a) / 1000000)

def replayMap (constMap : Std.HashMap Name ConstantInfo) (timingLog : Option IO.FS.Handle := none) : IO State := do
  let mut remaining : NameSet := ∅
  let mut nUnsafe := 0
  for (n, ci) in constMap.toList do
    if !ci.isUnsafe && !ci.isPartial then remaining := remaining.insert n
    else nUnsafe := nUnsafe + 1
  IO.println s!"REPLAY_TODO {remaining.size} skipped_unsafe_partial={nUnsafe}"
  let env ← mkEmptyEnvironment
  let t1 ← IO.monoNanosNow
  let (_, s) ← StateRefT'.run (s := { env := env.toKernelEnv, remaining, lastReport := t1, startNs := t1 : State }) do
    ReaderT.run (r := { newConstants := constMap, timingLog : Context }) do
      for n in remaining do
        replayConstant n
      checkPostponedConstructors
      checkPostponedRecursors
  return s

def replayMain (path out : String) : IO Unit := do
  let t0 ← IO.monoNanosNow
  let h ← IO.FS.Handle.mk path .read
  let sol ← Export.parseStream (IO.FS.Stream.ofHandle h)
  let t1 ← IO.monoNanosNow
  IO.println s!"PARSE_MS {ms t0 t1} consts={sol.constMap.size}"
  (← IO.getStdout).flush
  let constMap := sol.constMap.erase `Quot.mk |>.erase `Quot.lift |>.erase `Quot.ind
  let timingLog ← IO.FS.Handle.mk (out ++ ".partial.tsv") .write
  let s ← replayMap constMap (some timingLog)
  let t2 ← IO.monoNanosNow
  IO.println s!"REPLAY_MS {ms t1 t2} decls={s.timings.size} kernel_sum_ms={s.totalNs / 1000000}"
  let sorted := s.timings.qsort (fun a b => a.2.2 > b.2.2)
  let mut lines : Array String := #[]
  for (n, k, ns) in sorted do
    lines := lines.push s!"{ns / 1000}\t{k}\t{n}"
  IO.FS.writeFile out (String.intercalate "\n" lines.toList ++ "\n")
  for (n, k, ns) in sorted[:40] do
    IO.println s!"  {ns / 1000000} ms  {k}  {n}"

def modmapMain (mod inp out : String) : IO Unit := do
  initSearchPath (← findSysroot)
  let env ← importModules #[{ module := mod.toName }] {}
  let mut m : Std.HashMap String String := {}
  for (n, _) in env.constants.toList do
    if let some idx := env.getModuleIdxFor? n then
      m := m.insert (toString n) (toString env.header.moduleNames[idx.toNat]!)
  let lines := (← IO.FS.readFile inp).splitOn "\n"
  let mut outL : Array String := #[]
  for l in lines do
    if l.isEmpty then continue
    let parts := l.splitOn "\t"
    let nm := parts.getD 2 ""
    outL := outL.push s!"{l}\t{m.getD nm "?"}"
  IO.FS.writeFile out (String.intercalate "\n" outL.toList ++ "\n")

def selfTest : IO Unit := do
  let env ← mkEmptyEnvironment
  let decl := Declaration.inductDecl [] 0
    [{ name := `RPFixture, type := mkSort (.succ .zero),
       ctors := [{ name := `RPFixture.mk, type := mkConst `RPFixture }] }] false
  let env ← match env.toKernelEnv.addDeclCore 0 0 decl (cancelTk? := none) with
    | .ok value => pure value
    | .error _ => throw <| IO.userError "Cannot construct replay test fixture"
  let mut constants : Std.HashMap Name ConstantInfo := {}
  for name in #[`RPFixture, `RPFixture.mk, `RPFixture.rec] do
    let some ci := env.find? name | throw <| IO.userError s!"Missing fixture {name}"
    constants := constants.insert name ci
  discard <| replayMap constants
  let expectReject (label : String) (bad : Std.HashMap Name ConstantInfo) : IO Unit := do
    let rejected ← try
      discard <| replayMap bad
      pure false
    catch _ => pure true
    unless rejected do throw <| IO.userError s!"Invalid {label} was accepted"
  let some (.ctorInfo ctor) := constants[`RPFixture.mk]?
    | throw <| IO.userError "Missing constructor fixture"
  expectReject "constructor" <| constants.insert ctor.name (.ctorInfo { ctor with numParams := 1 })
  let some (.recInfo recursor) := constants[`RPFixture.rec]?
    | throw <| IO.userError "Missing recursor fixture"
  expectReject "recursor" <| constants.insert recursor.name (.recInfo { recursor with numParams := 1 })
  let invalid : ConstantInfo := .thmInfo { name := `RPBad, levelParams := [], type := mkConst `RPFixture, value := mkSort .zero }
  expectReject "proof" <| constants.insert `RPBad invalid
  IO.println "Replay self-test passed (valid inductive; invalid constructor, recursor and proof rejected)."

end RP

def main (args : List String) : IO Unit := do
  match args with
  | ["self-test"] => RP.selfTest
  | ["replay", p, o] => RP.replayMain p o
  | ["modmap", mod, i, o] => RP.modmapMain mod i o
  | _ => throw <| .userError "usage: replayprof self-test | replay <export> <out.tsv> | modmap <Module> <in> <out>"
