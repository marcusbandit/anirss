class Anirss < Formula
  desc "Search nyaa.si and hand the result to qBittorrent"
  homepage "https://github.com/marcusbandit/anirss"
  url "https://github.com/marcusbandit/anirss/archive/refs/tags/v0.2.3.tar.gz"
  sha256 "93270febecd6d883a9e5b3a9e6fb13a4ed1e7cb563951ca7487b6d1984d6ea8a"
  license "GPL-3.0-or-later"
  head "https://github.com/marcusbandit/anirss.git", branch: "main"

  depends_on "fzf"
  depends_on "python@3.13"

  def install
    python = Formula["python@3.13"].opt_bin/"python3.13"
    inreplace "anirss", "#!/usr/bin/env python3", "#!#{python}"
    bin.install "anirss"
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
