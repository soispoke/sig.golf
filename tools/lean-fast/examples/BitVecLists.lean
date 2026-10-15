import Lean.Elab.Term

/-!
Large program images contain hundreds of thousands of fixed-width numerals.
Elaborate these lists directly instead of resolving `OfNat` for every element.
The result is an ordinary list of `BitVec.ofNat` terms, checked by Lean's kernel;
no evaluation, external data, or additional axioms are involved.
-/

open Lean Elab Term in
elab "bv_list% " width:num " [" values:num,* "]" : term => do
  let w := mkNatLit width.getNat
  let ty := mkApp (mkConst ``BitVec) w
  let xs := values.getElems.map fun n =>
    mkApp2 (mkConst ``BitVec.ofNat) w (mkNatLit n.getNat)
  return xs.foldr (fun x tail => mkApp3 (mkConst ``List.cons [0]) ty x tail)
    (mkApp (mkConst ``List.nil [0]) ty)

-- Ordinary kernel-checked equalities, including fixed-width truncation.
example : (bv_list% 32 [0, 1, 0xffffffff, 0x100000000] : List (BitVec 32)) =
    [0, 1, 4294967295, 0] := by decide +kernel
example : (bv_list% 8 [] : List (BitVec 8)) = [] := rfl
example : (bv_list% 8 [255, 256, 257] : List (BitVec 8)) = [255, 0, 1] := by decide +kernel
