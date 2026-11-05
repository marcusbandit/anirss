class Anirss < Formula
  desc "Search the endpoints you add and hand releases to qBittorrent"
  homepage "https://github.com/marcusbandit/anirss"
  url "https://github.com/marcusbandit/anirss/archive/refs/tags/v0.7.0.tar.gz"
  sha256 "1fd2030a963bc6e5c1a6e5491fdbc3d78e11f6f17f1f056bb016b72d5fa8bd03"
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
