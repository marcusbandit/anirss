class Anirss < Formula
  desc "Search nyaa.si and hand the result to qBittorrent"
  homepage "https://github.com/marcusbandit/anirss"
  url "https://github.com/marcusbandit/anirss/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "341d7fd355f090dd4c23830ae000f0c965048934b8446872febcdd1c32d13776"
  license "GPL-3.0-or-later"
  head "https://github.com/marcusbandit/anirss.git", branch: "main"

  depends_on "fzf"
  depends_on "python@3.13"

  def install
    python = Formula["python@3.13"].opt_bin/"python3.13"
    inreplace "anirss", "#!/usr/bin/env python3", "#!#{python}"
    bin.install "anirss"
  end

  test do
    assert_equal "anirss #{version}", shell_output("#{bin}/anirss --version").strip
  end
end
