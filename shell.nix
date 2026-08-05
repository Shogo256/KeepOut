{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    gnat14
    gprbuild
    gfortran
    iverilog
    binutils
    python314
    nodejs_24
    yarn
    jdk21
    gnuapl
    solc
    rustc
    cargo
    llvm_19
    tlaplus
    tlaps
    j
    guile
    pixi
  ];

  shellHook = ''
    export PATH="$HOME/.pixi/bin:/usr/local/bin:$PATH"

    export CHPL_LLVM=none
    if [ -f "$HOME/chapel/util/setchplenv.bash" ]; then
      source "$HOME/chapel/util/setchplenv.bash" > /dev/null 2>&1
    fi

    function tlc {
      local jar_path="${pkgs.tlaplus}/share/java/tla2tools.jar"
      if [ "$1" = "--version" ]; then
        java -cp "$jar_path" tlc2.TLC 2>&1 | head -n 1
      else
        java -cp "$jar_path" tlc2.TLC "$@"
      fi
    }
    export -f tlc

    function cloak-run { ./run_cloak_all.sh "$@"; }
    function cloak-tvla { python3 scripts/evaluate_vcd_tvla.py "$@"; }
    function cloak-cpa { python3 scripts/evaluate_cloak.py "$@"; }
    function cloak-noise { python3 scripts/evaluate_simulink_noise.py "$@"; }
    export -f cloak-run cloak-tvla cloak-cpa cloak-noise

    echo "Development environment loaded successfully."
    echo "Mojo:   $(mojo --version 2>/dev/null || echo 'not found')"
    echo "SaC:    $(sac2c -V | head -n 1 2>/dev/null || echo 'not found')"
    echo "Chapel: $(chpl --version | head -n 1 2>/dev/null || echo 'not found')"
    echo "Cloak:  Commands registered (cloak-run, cloak-tvla, cloak-cpa, cloak-noise)"
  '';
}
