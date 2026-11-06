class Anirss < Formula
  desc "Search the endpoints you add and hand releases to qBittorrent"
  homepage "https://github.com/marcusbandit/anirss"
  url "https://github.com/marcusbandit/anirss/archive/refs/tags/v0.7.1.tar.gz"
  sha256 "ffa15a2292fa70e134387bd55fadb1188cbcc1baa406d1a8920a6faf6899f6a8"
  license "GPL-3.0-or-later"
  head "https://github.com/marcusbandit/anirss.git", branch: "main"

  depends_on "fzf"
  depends_on "python@3.13"

  def install
    python = Formula["python@3.13"].opt_bin/"python3.13"
    inreplace "anirss", "#!/usr/bin/env python3", "#!#{python}"
    # Launcher and library live side-by-side; the launcher uses
    # `os.path.realpath(__file__)` to find anirss_lib next to itself.
    libexec.install "anirss", "anirss_lib"
    bin.install_symlink libexec/"anirss"
    zsh_completion.install "completions/_anirss"
    bash_completion.install "completions/anirss.bash" => "anirss"
  end

  test do
    assert_equal "anirss #{version}", shell_output("#{bin}/anirss --version").strip
    # Completion files were installed.
    assert_predicate zsh_completion/"_anirss", :exist?
    assert_predicate bash_completion/"anirss", :exist?
  end
end
