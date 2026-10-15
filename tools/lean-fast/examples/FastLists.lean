/-
Adapted from the existing candidate optimization at commit 44f3494.
Direct recursors for kernel reduction, with general equivalence proofs.
These are optional helpers; their speed depends on the surrounding proof.
-/
import Lean

namespace FastList

/-- `l.drop n` by `Nat.rec` (tail thunks are shared, so repeated drops hit the whnf cache). -/
noncomputable def dropK {α : Type} (n : Nat) (l : List α) : List α :=
  Nat.rec (motive := fun _ => List α → List α) (fun l => l) (fun _ ih l => ih l.tail) n l

theorem dropK_eq {α : Type} (n : Nat) (l : List α) : dropK n l = l.drop n := by
  induction n generalizing l with
  | zero => rfl
  | succ n ih =>
    show dropK n l.tail = _
    rw [ih]
    cases l <;> simp

/-- `l₁ ++ l₂` by `List.rec`. -/
noncomputable def appendK {α : Type} (l₁ l₂ : List α) : List α :=
  List.rec (motive := fun _ => List α) l₂ (fun a _ ih => a :: ih) l₁

theorem appendK_eq {α : Type} (l₁ l₂ : List α) : appendK l₁ l₂ = l₁ ++ l₂ := by
  induction l₁ with
  | nil => rfl
  | cons a t ih => show a :: appendK t l₂ = _; rw [ih]; rfl

/-- `ls.flatten` by `List.rec` (later lists are only unfolded when reached). -/
noncomputable def flattenK {α : Type} (ls : List (List α)) : List α :=
  List.rec (motive := fun _ => List α) [] (fun c _ ih => appendK c ih) ls

theorem flattenK_eq {α : Type} (ls : List (List α)) : flattenK ls = ls.flatten := by
  induction ls with
  | nil => rfl
  | cons c t ih => show appendK c (flattenK t) = _; rw [appendK_eq, ih]; rfl

/-- `l[n]?` through `dropK`. -/
noncomputable def getK {α : Type} (n : Nat) (l : List α) : Option α := (dropK n l).head?

theorem getK_eq {α : Type} (n : Nat) (l : List α) : getK n l = l[n]? := by
  unfold getK
  rw [dropK_eq]
  induction n generalizing l with
  | zero => cases l <;> rfl
  | succ n ih =>
    cases l with
    | nil => rfl
    | cons a t => rw [List.drop_succ_cons, ih t, List.getElem?_cons_succ]
end FastList

example : FastList.dropK 3 ([1, 2] : List Nat) = [] := by decide +kernel
example : FastList.getK 1 ([4, 5, 6] : List Nat) = some 5 := by decide +kernel
example : FastList.getK 3 ([4, 5, 6] : List Nat) = none := by decide +kernel
example : FastList.flattenK ([[1], [], [2, 3]] : List (List Nat)) = [1, 2, 3] := by
  decide +kernel

open Lean Elab Command in
run_cmd do
  for name in #[`FastList.dropK_eq, `FastList.appendK_eq, `FastList.flattenK_eq, `FastList.getK_eq] do
    for ax in ← collectAxioms name do
      unless #[`propext, `Quot.sound, `Classical.choice].contains ax do
        throwError "Unexpected axiom {ax} in {name}"
