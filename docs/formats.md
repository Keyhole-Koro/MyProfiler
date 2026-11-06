# File formats

## Symbol map: `<image>.mbin.map`

MyLinker writes one symbol per line in address order. The header gives the
text range, which is how a reader tells code from data.

```text
# MyLinker symbol map
# text: 0x20000 .. 0x20b74 (2932 bytes)
# data: 0x21000 .. 0x2109c (156 bytes)
0x00020010 app_main
```

Every public text and data symbol of a linked object appears. A private symbol
appears only if something referenced it, tagged `name-[module:n]`.

## Line table: `<image>.mbin.lines`

MyLinker writes it from the objects' debug trailers. Lines start with a kind.

```text
# MyLinker line table
file 0 /src/main.mln
func 0x00000040 0 main
line 0x00000040 0 12 frame
line 0x00000058 0 13 -
```

| Kind | Fields | Meaning |
|---|---|---|
| `file` | id, path | A source file. The path is the rest of the line. |
| `func` | address, file id, name | A function starts here. The name is the rest of the line. |
| `line` | address, file id, line, cost | This row covers the code up to the next row. |

The cost is `-` for ordinary code, or one of:

| Cost | Code |
|---|---|
| `frame` | Prologue and epilogue |
| `copy` | An aggregate copy |
| `check` | A bounds check |
| `arith` | A multiply, divide or modulo loop |
| `drop` | A drop |

## Assembly directives

`mlc --line-info` writes these into the `.masm`. They emit no code, so an image
is the same with or without them.

```text
  .func "main" "/src/main.mln"   ; a function starts here, in this file
  .loc 12                        ; the code below came from line 12
  .cost "copy"                   ; a cost region starts; "" returns to none
```

`.func` also resets the line and the cost.

## Profile: `myemu --profile`

JSON with `"format": 2`. Every percentage in a report is out of
`total_instructions`.

| Key | Rows |
|---|---|
| `spaces` | `space`, `instructions`, `text_sample` |
| `threads` | `thread`, `instructions` |
| `pc_hits` | `space`, `pc`, `hits` |
| `opcode_hits` | `opcode`, `mnemonic`, `count` |
| `mem_reads`, `mem_writes` | `page`, `count` |
| `functions` | `space`, `entry`, `self`, `inclusive`, `calls` |
| `edges` | `caller_space`, `caller`, `callee_space`, `callee`, `count` |

Space 0 is the kernel, or the whole program without an MMU. Any other space is a
user process: its page directory's address, plus in the low 12 bits how many
programs that directory held before. `text_sample` is the hex of the first text
page, which a reader matches against the built images.

`thread` is the value of the kernel's current-thread word, or -1 when it holds
"none running" and -2 before the kernel first wrote it.

`inclusive` counts instructions of the same thread only, and counts a recursive
function once.

## Benchmark table: `bench`

```json
{"format": 1, "total": 2479903423, "cases": {"moveParam": 1234}}
```
