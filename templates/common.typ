#let setup(doc) = {
  set page(paper: "a4", margin: (x: 1.5cm, y: 1.3cm))
  set text(font: "Carlito", size: 10.5pt, lang: "en", region: "gb")
  set par(leading: 0.42em, spacing: 0.55em)
  show link: it => underline(text(fill: rgb("#1a0dab"), it))
  doc
}

#let header(d) = align(center)[
  #block(below: 0.5em, text(size: 22pt, d.name))
  #{
    let c = d.contact
    let parts = (
      link("mailto:" + c.email, c.email),
      link("https://" + c.github, c.github),
      link(c.portfolio, "Portfolio"),
      ..c.phones.map(p => [#p]),
    )
    parts.join(" | ")
  }
]

#let section(title) = block(
  above: 1.1em, below: 0.6em, width: 100%, inset: (bottom: 2pt), stroke: (bottom: 0.6pt),
  text(size: 12.5pt, title),
)
