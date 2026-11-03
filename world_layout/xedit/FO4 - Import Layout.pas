{
  FO4 - Import Layout
  ===================
  Reads layout.tsv written by the Unreal / Unity layout exporters
  (Mossy blender add-on, world_layout/) and places the objects into an
  existing exterior worldspace as new REFRs in a layout plugin.

  Install: copy this file into  <xEdit folder>\Edit Scripts\
  Run:     load your landscape plugin (and the layout plugin if it already
           exists), right-click any record > Apply Script > "FO4 - Import Layout".

  What it does
    * Creates one STAT per new NIF (EditorID from the layout file), with
      OBND bounds from the engine mesh.  Existing STATs with that EditorID
      in the target plugin are reused, so re-imports don't duplicate.
    * Places each object as a REFR in the right exterior cell (cell override
      copied into the target plugin).  Every REFR gets an EditorID carrying
      the engine actor's stable hash, so RE-RUNNING THE IMPORT UPDATES the
      refs it made last time instead of adding duplicates.
    * Refs from an earlier import that are no longer in the layout are only
      REPORTED, never deleted -- delete them yourself if that was intended.
    * Objects that moved to a different cell since the last import are
      re-created in the new cell (the old copy is one of OUR records in OUR
      plugin, so removing it loses nothing).

  Set DRY_RUN to True to only report what would happen.
}
unit FO4ImportLayout;

const
  DRY_RUN = False;
  CELL_SIZE = 4096;

var
  Rows: TStringList;
  ColNames: TStringList;     // column names from the TSV header row
  Hdr: TStringList;          // name=value from the "#key<TAB>value" lines
  Done: TStringList;         // ids already handled (updated in place)
  CellRows: TStringList;     // "x,y" = comma separated row indices
  Target: IInterface;
  nCreated, nUpdated, nMoved, nStats, nErrors, nOrphans: integer;

// ---------------------------------------------------------------- helpers

function Field(line: string; index: integer): string;
var
  sl: TStringList;
begin
  sl := TStringList.Create;
  try
    sl.Delimiter := #9;
    sl.StrictDelimiter := True;
    sl.DelimitedText := line;
    if index < sl.Count then Result := sl[index] else Result := '';
  finally
    sl.Free;
  end;
end;

function F(rowIdx: integer; name: string): string;
var
  ci: integer;
begin
  ci := ColNames.IndexOf(name);
  if ci < 0 then begin
    Result := '';
    Exit;
  end;
  Result := Field(Rows[rowIdx], ci);
end;

function FindFileByName(fname: string): IInterface;
var
  i: integer;
begin
  Result := nil;
  for i := 0 to FileCount - 1 do
    if SameText(GetFileName(FileByIndex(i)), fname) then begin
      Result := FileByIndex(i);
      Exit;
    end;
end;

function FindWorldspace(edid: string): IInterface;
var
  i: integer;
  g, r: IInterface;
begin
  Result := nil;
  for i := 0 to FileCount - 1 do begin
    g := GroupBySignature(FileByIndex(i), 'WRLD');
    if not Assigned(g) then Continue;
    r := MainRecordByEditorID(g, edid);
    if Assigned(r) then begin
      Result := MasterOrSelf(r);
      Exit;
    end;
  end;
end;

function FloorDiv(v: double): integer;
begin
  Result := Trunc(v / CELL_SIZE);
  if (Result * CELL_SIZE) > v then Result := Result - 1;
end;

function CellKeyFromPos(x, y: double): string;
begin
  Result := IntToStr(FloorDiv(x)) + ',' + IntToStr(FloorDiv(y));
end;

// Base given as "Plugin.esm|00ABCDEF" or as an EditorID.
function FindBase(spec: string): IInterface;
var
  p, i, j: integer;
  f, g, r: IInterface;
  fname, sigs: string;
  sigList: TStringList;
  local: cardinal;
begin
  Result := nil;
  p := Pos('|', spec);
  if p > 0 then begin
    fname := Copy(spec, 1, p - 1);
    f := FindFileByName(fname);
    if not Assigned(f) then Exit;
    local := StrToInt('$' + Copy(spec, p + 1, 8)) and $00FFFFFF;
    // file-relative FormID: the file's own records use index = master count
    Result := RecordByFormID(f, (MasterCount(f) shl 24) or local, True);
    Exit;
  end;
  sigList := TStringList.Create;
  try
    sigList.CommaText := 'STAT,SCOL,MSTT,FURN,CONT,ACTI,LIGH,MISC,TREE,FLOR,DOOR,TXST';
    for i := 0 to FileCount - 1 do
      for j := 0 to sigList.Count - 1 do begin
        g := GroupBySignature(FileByIndex(i), sigList[j]);
        if not Assigned(g) then Continue;
        r := MainRecordByEditorID(g, spec);
        if Assigned(r) then begin
          Result := MasterOrSelf(r);
          Exit;
        end;
      end;
  finally
    sigList.Free;
  end;
end;

function GetOrCreateStat(rowIdx: integer): IInterface;
var
  g, s: IInterface;
  edid: string;
begin
  edid := F(rowIdx, 'stat_edid');
  Result := nil;
  if not Assigned(Target) then begin
    AddMessage('  [dry run] would create STAT ' + edid + ' -> Meshes\' + F(rowIdx, 'nif'));
    Exit;
  end;
  g := GroupBySignature(Target, 'STAT');
  if Assigned(g) then begin
    s := MainRecordByEditorID(g, edid);
    if Assigned(s) then begin
      Result := s;
      Exit;
    end;
  end;
  if DRY_RUN then begin
    AddMessage('  [dry run] would create STAT ' + edid + ' -> Meshes\' + F(rowIdx, 'nif'));
    Result := nil;
    Exit;
  end;
  if not Assigned(g) then g := Add(Target, 'STAT', True);
  s := Add(g, 'STAT', True);
  SetElementEditValues(s, 'EDID', edid);
  SetElementEditValues(s, 'Model\MODL', F(rowIdx, 'nif'));
  SetElementEditValues(s, 'OBND\X1', F(rowIdx, 'bminx'));
  SetElementEditValues(s, 'OBND\Y1', F(rowIdx, 'bminy'));
  SetElementEditValues(s, 'OBND\Z1', F(rowIdx, 'bminz'));
  SetElementEditValues(s, 'OBND\X2', F(rowIdx, 'bmaxx'));
  SetElementEditValues(s, 'OBND\Y2', F(rowIdx, 'bmaxy'));
  SetElementEditValues(s, 'OBND\Z2', F(rowIdx, 'bmaxz'));
  if GetElementEditValues(s, 'Model\MODL') = '' then begin
    AddMessage('  ERROR: STAT ' + edid + ' was created but its model path did not stick');
    Inc(nErrors);
  end;
  Inc(nStats);
  Result := s;
end;

function ResolveBase(rowIdx: integer): IInterface;
var
  spec: string;
begin
  spec := F(rowIdx, 'base');
  if spec <> '' then begin
    Result := FindBase(spec);
    if not Assigned(Result) then begin
      AddMessage('  ERROR: base "' + spec + '" not found for ' + F(rowIdx, 'ref_edid'));
      Inc(nErrors);
    end;
  end else
    Result := GetOrCreateStat(rowIdx);
end;

procedure WriteRef(ref, base: IInterface; rowIdx: integer);
var
  sc: string;
begin
  SetElementEditValues(ref, 'EDID', F(rowIdx, 'ref_edid'));
  AddRequiredElementMasters(base, Target, False);
  SetElementNativeValues(ref, 'NAME', GetLoadOrderFormID(base));
  SetElementEditValues(ref, 'DATA\Position\X', F(rowIdx, 'px'));
  SetElementEditValues(ref, 'DATA\Position\Y', F(rowIdx, 'py'));
  SetElementEditValues(ref, 'DATA\Position\Z', F(rowIdx, 'pz'));
  // edit values are degrees (xEdit stores radians on disk)
  SetElementEditValues(ref, 'DATA\Rotation\X', F(rowIdx, 'rx'));
  SetElementEditValues(ref, 'DATA\Rotation\Y', F(rowIdx, 'ry'));
  SetElementEditValues(ref, 'DATA\Rotation\Z', F(rowIdx, 'rz'));
  sc := F(rowIdx, 'scale');
  if (sc <> '1.0000') and (sc <> '') then
    SetElementEditValues(ref, 'XSCL', sc)
  else if ElementExists(ref, 'XSCL') then
    RemoveElement(ref, 'XSCL');
  if GetElementEditValues(ref, 'NAME') = '' then begin
    AddMessage('  ERROR: ' + F(rowIdx, 'ref_edid') + ' has no base after writing');
    Inc(nErrors);
  end;
end;

function RowById(id: string): integer;
var
  i, ci: integer;
begin
  Result := -1;
  ci := ColNames.IndexOf('ref_edid');
  for i := 0 to Rows.Count - 1 do
    if Field(Rows[i], ci) = id then begin
      Result := i;
      Exit;
    end;
end;

procedure PrepareBases;
var
  i: integer;
  seen: TStringList;
  k: string;
begin
  seen := TStringList.Create;
  try
    for i := 0 to Rows.Count - 1 do begin
      k := F(i, 'base') + '|' + F(i, 'stat_edid');
      if seen.IndexOf(k) >= 0 then Continue;
      seen.Add(k);
      ResolveBase(i);
    end;
  finally
    seen.Free;
  end;
end;

// Pass 1: refs this script made on an earlier run (found by EDID prefix).
procedure UpdateExistingRefs;
var
  i, r: integer;
  rec, base: IInterface;
  edid, prefix, newKey, oldKey: string;
begin
  prefix := Hdr.Values['prefix'] + '_';
  for i := RecordCount(Target) - 1 downto 0 do begin
    rec := RecordByIndex(Target, i);
    if Signature(rec) <> 'REFR' then Continue;
    edid := GetElementEditValues(rec, 'EDID');
    if Pos(prefix, edid) <> 1 then Continue;
    r := RowById(edid);
    if r < 0 then begin
      AddMessage('  orphan (not in layout any more, left alone): ' + edid);
      Inc(nOrphans);
      Continue;
    end;
    oldKey := CellKeyFromPos(GetElementNativeValues(rec, 'DATA\Position\X'),
                             GetElementNativeValues(rec, 'DATA\Position\Y'));
    newKey := F(r, 'cell_x') + ',' + F(r, 'cell_y');
    if oldKey <> newKey then begin
      // moved to another cell: drop our old record, pass 2 re-creates it
      if DRY_RUN then
        AddMessage('  [dry run] would move ' + edid + ' from cell ' + oldKey + ' to ' + newKey)
      else
        RemoveNode(rec);
      Inc(nMoved);
      Continue;
    end;
    base := ResolveBase(r);
    if not Assigned(base) then Continue;
    if not DRY_RUN then WriteRef(rec, base, r);
    Done.Add(edid);
    Inc(nUpdated);
  end;
end;

function GetOrOverrideCell(cell: IInterface): IInterface;
var
  k: integer;
  o: IInterface;
begin
  for k := 0 to OverrideCount(cell) - 1 do begin
    o := OverrideByIndex(cell, k);
    if SameText(GetFileName(GetFile(o)), GetFileName(Target)) then begin
      Result := o;
      Exit;
    end;
  end;
  if SameText(GetFileName(GetFile(cell)), GetFileName(Target)) then begin
    Result := cell;
    Exit;
  end;
  AddRequiredElementMasters(cell, Target, False);
  Result := wbCopyElementToFile(cell, Target, False, True);
end;

procedure PlaceRowsInCell(cell: IInterface; key: string);
var
  idxs: TStringList;
  i, r: integer;
  c, ref, base: IInterface;
begin
  idxs := TStringList.Create;
  try
    idxs.CommaText := CellRows.Values[key];
    c := nil;
    for i := 0 to idxs.Count - 1 do begin
      r := StrToInt(idxs[i]);
      if Done.IndexOf(F(r, 'ref_edid')) >= 0 then Continue;
      base := ResolveBase(r);
      if not Assigned(base) and not DRY_RUN then Continue;
      if DRY_RUN then begin
        AddMessage('  [dry run] would place ' + F(r, 'ref_edid') + ' in cell ' + key);
        Done.Add(F(r, 'ref_edid'));
        Inc(nCreated);
        Continue;
      end;
      if not Assigned(c) then c := GetOrOverrideCell(cell);
      ref := Add(c, 'REFR', True);
      if not Assigned(ref) then begin
        AddMessage('  ERROR: could not add a REFR to cell ' + key);
        Inc(nErrors);
        Continue;
      end;
      WriteRef(ref, base, r);
      Done.Add(F(r, 'ref_edid'));
      Inc(nCreated);
    end;
  finally
    idxs.Free;
  end;
end;

// Pass 2: walk the worldspace's exterior blocks; place rows in matching cells.
procedure WalkGroup(g: IInterface);
var
  i: integer;
  e: IInterface;
  key: string;
begin
  for i := 0 to ElementCount(g) - 1 do begin
    e := ElementByIndex(g, i);
    if ElementType(e) = etGroupRecord then begin
      if (GroupType(e) = 4) or (GroupType(e) = 5) then WalkGroup(e);
    end else if (Signature(e) = 'CELL') and (GroupType(g) = 5) then begin
      if not ElementExists(e, 'XCLC') then Continue;
      key := IntToStr(GetElementNativeValues(e, 'XCLC\X')) + ',' +
             IntToStr(GetElementNativeValues(e, 'XCLC\Y'));
      if CellRows.IndexOfName(key) >= 0 then begin
        PlaceRowsInCell(e, key);
        CellRows.Delete(CellRows.IndexOfName(key));
      end;
    end;
  end;
end;

// ---------------------------------------------------------------- main

function Initialize: integer;
var
  dlg: TOpenDialog;
  all: TStringList;
  i: integer;
  line, key, wsName, plugName: string;
  wrld: IInterface;
begin
  Result := 0;
  nCreated := 0; nUpdated := 0; nMoved := 0; nStats := 0; nErrors := 0; nOrphans := 0;

  dlg := TOpenDialog.Create(nil);
  try
    dlg.Title := 'Choose layout.tsv';
    dlg.Filter := 'FO4 layout (*.tsv)|*.tsv';
    if not dlg.Execute then begin
      Result := 1;
      Exit;
    end;
    all := TStringList.Create;
    all.LoadFromFile(dlg.FileName);
    AddMessage('Layout: ' + dlg.FileName);
  finally
    dlg.Free;
  end;

  Hdr := TStringList.Create;
  Rows := TStringList.Create;
  ColNames := TStringList.Create;
  ColNames.Delimiter := #9;
  ColNames.StrictDelimiter := True;
  Done := TStringList.Create;
  CellRows := TStringList.Create;
  try
    for i := 0 to all.Count - 1 do begin
      line := all[i];
      if line = '' then Continue;
      if Copy(line, 1, 1) = '#' then
        Hdr.Values[Copy(Field(line, 0), 2, 100)] := Field(line, 1)
      else if ColNames.Count = 0 then
        ColNames.DelimitedText := line
      else
        Rows.Add(line);
    end;
    all.Free;

    if Hdr.Values['format'] <> 'fo4-layout' then begin
      AddMessage('Not a fo4-layout file.');
      Result := 1;
      Exit;
    end;
    AddMessage(Format('%d placement(s) from %s', [Rows.Count, Hdr.Values['source']]));

    wsName := Hdr.Values['worldspace'];
    wrld := FindWorldspace(wsName);
    if not Assigned(wrld) then begin
      AddMessage('Worldspace "' + wsName + '" is not loaded -- load the landscape plugin.');
      Result := 1;
      Exit;
    end;
    AddMessage('Worldspace: ' + Name(wrld));

    plugName := Hdr.Values['plugin'];
    Target := FindFileByName(plugName);
    if not Assigned(Target) then begin
      if DRY_RUN then begin
        AddMessage('[dry run] target plugin ' + plugName + ' not loaded; it would be created.');
      end else begin
        AddMessage('Target plugin ' + plugName + ' is not loaded -- name the new file "' +
                   plugName + '" in the next prompt.');
        Target := AddNewFile;
        if not Assigned(Target) then begin
          Result := 1;
          Exit;
        end;
      end;
    end;
    if Assigned(Target) then begin
      AddMasterIfMissing(Target, 'Fallout4.esm');
      AddMasterIfMissing(Target, GetFileName(GetFile(wrld)));
      AddMessage('Target plugin: ' + GetFileName(Target));
    end;

    // group rows by cell
    for i := 0 to Rows.Count - 1 do begin
      key := F(i, 'cell_x') + ',' + F(i, 'cell_y');
      if CellRows.IndexOfName(key) < 0 then
        CellRows.Values[key] := IntToStr(i)
      else
        CellRows.Values[key] := CellRows.Values[key] + ',' + IntToStr(i);
    end;
    AddMessage(Format('Spans %d cell(s)', [CellRows.Count]));

    // Create every new STAT up front, so the passes below never add records
    // to the target plugin while iterating over it by index.
    PrepareBases;
    if Assigned(Target) then UpdateExistingRefs;
    WalkGroup(ChildGroup(wrld));

    for i := 0 to CellRows.Count - 1 do begin
      AddMessage('  ERROR: cell ' + CellRows.Names[i] +
                 ' does not exist in the worldspace -- those objects were NOT placed');
      Inc(nErrors);
    end;

    AddMessage('---------------------------------------------');
    if DRY_RUN then AddMessage('DRY RUN -- nothing was written');
    AddMessage(Format('Placed %d new, updated %d, moved %d, new STATs %d, orphans %d, errors %d',
      [nCreated, nUpdated, nMoved, nStats, nOrphans, nErrors]));
  finally
    Hdr.Free; Rows.Free; ColNames.Free; Done.Free; CellRows.Free;
  end;
end;

end.
